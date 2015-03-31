from bloop.expression import (
    AttributeExists,
    BeginsWith,
    Comparison,
    Contains,
    Between,
    In
)
import operator
import declare
import uuid
missing = object()
_meta_key = "__column_meta_{}".format(uuid.uuid4().hex)


class ComparisonMixin(object):
    def __hash__(self):
        # With single inheritance this looks stupid, but as a Mixin this
        # ensures we kick hashing back to the other base class so things
        # don't get fucked up, like `set()`.
        return super().__hash__()

    def __eq__(self, value):
        # Special case - None should use function attribute_not_exists
        if value is None:
            return AttributeExists(self, negate=True)
        comparator = operator.eq
        return Comparison(self, comparator, value)

    def __ne__(self, value):
        # Special case - None should use function attribute_exists
        if value is None:
            return AttributeExists(self, negate=False)
        comparator = operator.ne
        return Comparison(self, comparator, value)

    def __lt__(self, value):
        comparator = operator.lt
        return Comparison(self, comparator, value)

    def __gt__(self, value):
        comparator = operator.gt
        return Comparison(self, comparator, value)

    def __le__(self, value):
        comparator = operator.le
        return Comparison(self, comparator, value)

    def __ge__(self, value):
        comparator = operator.ge
        return Comparison(self, comparator, value)

    def is_(self, value):
        ''' alias for == '''
        return self == value

    def is_not(self, value):
        ''' alias for != '''
        return self != value

    def between(self, lower, upper):
        ''' lower <= column.value <= upper '''
        return Between(self, lower, upper)

    def in_(self, *values):
        ''' column.value in [3, 4, 5] '''
        return In(self, values)

    def begins_with(self, value):
        return BeginsWith(self, value)

    def contains(self, value):
        return Contains(self, value)


class Column(declare.Field, ComparisonMixin):
    def __init__(self, *args, hash_key=None, range_key=None,
                 name=missing, **kwargs):
        self._hash_key = hash_key
        self._range_key = range_key
        self._dynamo_name = name

        self.column_key = "__{}_{}".format(
            self.__class__.__name__, uuid.uuid4().hex)
        super().__init__(*args, **kwargs)

    def __str__(self):
        attrs = ["model_name", "dynamo_name", "hash_key", "range_key"]

        def _attr(attr):
            return "{}={}".format(attr, getattr(self, attr))
        attrs = ", ".join(_attr(attr) for attr in attrs)
        return "Column({})".format(attrs)

    @property
    def hash_key(self):
        return self._hash_key

    @property
    def range_key(self):
        return self._range_key

    @property
    def dynamo_name(self):
        if self._dynamo_name is missing:
            return self.model_name
        return self._dynamo_name

    def __meta__(self, obj):
        ''' Return the column-specific metadata dict for a given object '''
        meta = obj.__dict__.get(_meta_key, None)
        if meta is None:
            meta = obj.__dict__[_meta_key] = {}
        column_meta = meta.get(self.column_key, None)
        if column_meta is None:
            column_meta = meta[self.column_key] = {}
        return column_meta

    def meta_get(self, obj, name, default=missing):
        '''
        look up and return the value of a property in the column metadata,
        setting and return the default value if specified.

        if `default` is not set, KeyError is raised and the metadata dict is
        not mutated.
        '''
        obj_meta = self.__meta__(obj)
        value = obj_meta.get(name, missing)
        # Failed to find - either set and return default, or raise
        if value is missing:
            # Don't mutate on fail to find
            if default is missing:
                raise KeyError("Unknown column meta property {}".format(name))
            else:
                value = obj_meta[name] = default
        return value

    def meta_set(self, obj, name, value):
        obj_meta = self.__meta__(obj)
        obj_meta[name] = value
        return value


class Index(Column):
    def __init__(self, *args, projection='KEYS_ONLY', **kwargs):
        # TODO: Handle all projection types
        super().__init__(*args, **kwargs)
        self.projection = projection


class GlobalSecondaryIndex(Index):
    def __init__(self, *args, write_units=1, read_units=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_units = write_units
        self.read_units = read_units


class LocalSecondaryIndex(Index):
    ''' LSIs don't have individual read/write units '''
    pass


def is_column(field):
    return isinstance(field, Column)


def is_index(field):
    return isinstance(field, Index)


def is_local_index(index):
    return isinstance(index, LocalSecondaryIndex)


def is_global_index(index):
    return isinstance(index, GlobalSecondaryIndex)
