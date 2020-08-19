"""
FiftyOne dataset helper.

| Copyright 2017-2020, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from collections import OrderedDict
from functools import wraps
import numbers

from mongoengine.errors import InvalidQueryError
import numpy as np
import six

import fiftyone.core.fields as fof
from fiftyone.core.odm.dataset import SampleFieldDocument, DatasetDocument
from fiftyone.core.odm.document import BaseEmbeddedDocument
from fiftyone.core.odm.sample import default_sample_fields


def no_delete_default_field(func):
    """Wrapper for :func:`DatasetHelper.delete_field` that prevents deleting
    default fields of :class:`SampleDocument`.

    This is a decorator because the subclasses implement this as either an
    instance or class method.
    """

    @wraps(func)
    def wrapper(cls_or_self, field_name, *args, **kwargs):
        if field_name in default_sample_fields():
            raise ValueError("Cannot delete default field '%s'" % field_name)

        return func(cls_or_self, field_name, *args, **kwargs)

    return wrapper


class DatasetHelper(object):
    def __init__(self, sample_doc_cls):
        self._sample_doc_cls = sample_doc_cls

    @property
    def sample_collection_name(self):
        return self._sample_doc_cls._meta["collection"]

    @property
    def fields(self):
        return self._sample_doc_cls._fields

    @property
    def fields_ordered(self):
        return self._sample_doc_cls._fields_ordered

    @property
    def field_names(self):
        return tuple(
            f
            for f in self._get_fields_ordered(include_private=False)
            if f != "id"
        )

    def get_field_schema(
        self, ftype=None, embedded_doc_type=None, include_private=False
    ):
        """Returns a schema dictionary describing the fields of this dataset.

        If the sample belongs to a dataset, the schema will apply to all
        samples in the dataset.

        Args:
            ftype (None): an optional field type to which to restrict the
                returned schema. Must be a subclass of
                :class:`fiftyone.core.fields.Field`
            embedded_doc_type (None): an optional embedded document type to
                which to restrict the returned schema. Must be a subclass of
                :class:`fiftyone.core.odm.BaseEmbeddedDocument`
            include_private (False): whether to include fields that start with
                `_` in the returned schema

        Returns:
             a dictionary mapping field names to field types
        """
        if ftype is None:
            ftype = fof.Field

        if not issubclass(ftype, fof.Field):
            raise ValueError(
                "Field type %s must be subclass of %s" % (ftype, fof.Field)
            )

        if embedded_doc_type and not issubclass(
            ftype, fof.EmbeddedDocumentField
        ):
            raise ValueError(
                "embedded_doc_type should only be specified if ftype is a"
                " subclass of %s" % fof.EmbeddedDocumentField
            )

        d = OrderedDict()
        field_names = self._get_fields_ordered(include_private=include_private)
        for field_name in field_names:
            # pylint: disable=no-member
            field = self._sample_doc_cls._fields[field_name]
            if not isinstance(self._sample_doc_cls._fields[field_name], ftype):
                continue

            if embedded_doc_type and not issubclass(
                field.document_type, embedded_doc_type
            ):
                continue

            d[field_name] = field

        return d

    def add_field(
        self,
        field_name,
        ftype,
        embedded_doc_type=None,
        subfield=None,
        save=True,
    ):
        """Adds a new field to the sample.

        Args:
            field_name: the field name
            ftype: the field type to create. Must be a subclass of
                :class:`fiftyone.core.fields.Field`
            embedded_doc_type (None): the
                :class:`fiftyone.core.odm.BaseEmbeddedDocument` type of the
                field. Used only when ``ftype`` is an embedded
                :class:`fiftyone.core.fields.EmbeddedDocumentField`
            subfield (None): the type of the contained field. Used only when
                ``ftype`` is a :class:`fiftyone.core.fields.ListField` or
                :class:`fiftyone.core.fields.DictField`
        """
        # Additional arg `save` is to prevent saving the fields when reloading
        # a dataset from the database.

        if field_name in self.fields:
            raise ValueError("Field '%s' already exists" % field_name)

        field = _create_field(
            field_name,
            ftype,
            embedded_doc_type=embedded_doc_type,
            subfield=subfield,
        )

        self.fields[field_name] = field
        self._sample_doc_cls._fields_ordered += (field_name,)

        # Only set the attribute if it is a class
        setattr(self._sample_doc_cls, field_name, field)

        if save:
            # Update dataset meta class
            dataset_doc = DatasetDocument.objects.get(
                sample_collection_name=self.sample_collection_name
            )

            field = self.fields[field_name]
            sample_field = SampleFieldDocument.from_field(field)
            dataset_doc.sample_fields.append(sample_field)
            dataset_doc.save()

    def add_implied_field(self, field_name, value):
        """Adds the field to the sample, inferring the field type from the
        provided value.

        Args:
            field_name: the field name
            value: the field value
        """
        if field_name in self.fields:
            raise ValueError("Field '%s' already exists" % field_name)

        self.add_field(field_name, **_get_implied_field_kwargs(value))

    @no_delete_default_field
    def delete_field(self, field_name):
        """Deletes the field from the sample.

        If the sample is in a dataset, the field will be removed from all
        samples in the dataset.

        Args:
            field_name: the field name

        Raises:
            AttributeError: if the field does not exist
        """
        try:
            # Delete from all samples
            self._sample_doc_cls.objects.update(
                **{"unset__%s" % field_name: None}
            )
        except InvalidQueryError:
            raise AttributeError("Sample has no field '%s'" % field_name)

        # Remove from dataset
        del self.fields[field_name]
        self._sample_doc_cls._fields_ordered = tuple(
            fn for fn in self.fields_ordered if fn != field_name
        )
        delattr(self._sample_doc_cls, field_name)

        # Update dataset meta class
        dataset_doc = DatasetDocument.objects.get(
            sample_collection_name=self.sample_collection_name
        )
        dataset_doc.sample_fields = [
            sf for sf in dataset_doc.sample_fields if sf.name != field_name
        ]
        dataset_doc.save()

    def drop_collection(self, *args, **kwargs):
        return self._sample_doc_cls.drop_collection(*args, **kwargs)

    def construct_doc_from_dict(self, d, extended=False):
        doc = self._sample_doc_cls.from_dict(d, extended=extended)

        return doc

    def get_field(self, doc, field_name):
        field = self.fields[field_name]
        return field.__get__(doc, self._sample_doc_cls)

    def set_field(self, doc, field_name, value, create=False):
        """Sets the value of a field of the sample document.

        Args:
            field_name: the field name
            value: the field value
            create (False): whether to create the field if it does not exist

        Raises:
            ValueError: if ``field_name`` is not an allowed field name or does
                not exist and ``create == False``
        """
        if field_name.startswith("_"):
            raise ValueError(
                "Invalid field name: '%s'. Field names cannot start with '_'"
                % field_name
            )

        if field_name not in self.fields:
            if hasattr(doc, field_name):
                raise ValueError(
                    "Cannot use reserved keyword '%s'" % field_name
                )

            if not create:
                msg = "Sample does not have field '%s'." % field_name
                if value is not None:
                    # don't report this when clearing a field.
                    msg += " Use `create=True` to create a new field."
                raise ValueError(msg)

            self.add_implied_field(field_name, value)

        field = self.fields[field_name]
        return field.__set__(doc, value)

    def clear_field(self, doc, field_name):
        """Clears the value of a field of the sample.

        Args:
            field_name: the field name

        Raises:
            ValueError: if the field does not exist
        """
        self.set_field(doc, field_name, None, create=False)

    def _get_fields_ordered(self, include_private=False):
        if include_private:
            return self.fields_ordered
        return tuple(f for f in self.fields_ordered if not f.startswith("_"))


def _get_implied_field_kwargs(value):
    if isinstance(value, BaseEmbeddedDocument):
        return {
            "ftype": fof.EmbeddedDocumentField,
            "embedded_doc_type": type(value),
        }

    if isinstance(value, bool):
        return {"ftype": fof.BooleanField}

    if isinstance(value, six.integer_types):
        return {"ftype": fof.IntField}

    if isinstance(value, numbers.Number):
        return {"ftype": fof.FloatField}

    if isinstance(value, six.string_types):
        return {"ftype": fof.StringField}

    if isinstance(value, (list, tuple)):
        return {"ftype": fof.ListField}

    if isinstance(value, np.ndarray):
        if value.ndim == 1:
            return {"ftype": fof.VectorField}

        return {"ftype": fof.ArrayField}

    if isinstance(value, dict):
        return {"ftype": fof.DictField}

    raise TypeError("Unsupported field value '%s'" % type(value))


def _create_field(field_name, ftype, embedded_doc_type=None, subfield=None):
    if not issubclass(ftype, fof.Field):
        raise ValueError(
            "Invalid field type '%s'; must be a subclass of '%s'"
            % (ftype, fof.Field)
        )

    kwargs = {"db_field": field_name}

    if issubclass(ftype, fof.EmbeddedDocumentField):
        kwargs.update({"document_type": embedded_doc_type})
        kwargs["null"] = True
    elif issubclass(ftype, (fof.ListField, fof.DictField)):
        if subfield is not None:
            kwargs["field"] = subfield
    else:
        kwargs["null"] = True

    field = ftype(**kwargs)
    field.name = field_name

    return field