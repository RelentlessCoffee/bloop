import bloop.client
import bloop.condition
import bloop.exceptions
import bloop.filter
import bloop.index
import bloop.model
import bloop.tracking
import collections
import collections.abc
import declare

_MISSING = object()
DEFAULT_CONFIG = {
    "atomic": False,
    "consistent": False,
    "prefetch": 0,
    "strict": True,
}


def _list_of(objs):
    """ wrap single elements in a list """
    if isinstance(objs, str):  # pragma: no cover
        return [objs]
    elif isinstance(objs, collections.abc.Iterable):
        return objs
    else:
        return [objs]


def _value_of(column):
    """ _value_of({"S": "Space Invaders"}) -> "Space Invaders" """
    return next(iter(column.values()))


def _dump_key(engine, obj):
    """
    dump the hash (and range, if there is one) key(s) of an object into
    a dynamo-friendly format.

    returns {dynamo_name: {type: value} for dynamo_name in hash/range keys}
    """
    meta = obj.Meta
    hash_key, range_key = meta.hash_key, meta.range_key
    hash_value = getattr(obj, hash_key.model_name, _MISSING)
    if hash_value is _MISSING:
        raise ValueError(
            "Must specify a value for the hash attribute '{}'".format(
                hash_key.model_name))
    key = {
        hash_key.dynamo_name: engine._dump(hash_key.typedef, hash_value)}
    if range_key:
        range_value = getattr(obj, range_key.model_name, _MISSING)
        if range_value is _MISSING:
            raise ValueError(
                "Must specify a value for the range attribute '{}'".format(
                    range_key.model_name))
        key[range_key.dynamo_name] = engine._dump(
            range_key.typedef, range_value)
    return key


class LoadRequest:
    def __init__(self, engine):
        self.engine = engine
        self.indexed_objects = {}
        self.objects = set()
        self.table_keys = {}
        self.wire = {}

    def add(self, obj):
        if obj in self.objects:
            return
        table_name = obj.Meta.table_name
        table_exists = table_name in self.wire

        if not table_exists:
            self.wire[table_name] = {
                "Keys": [],
                "ConsistentRead": self.engine.config["consistent"]}
            self.indexed_objects[table_name] = {}

        # key is {dynamo_name: {dynamo_type: value}, ...} for hash/range keys
        key = _dump_key(self.engine, obj)
        self.wire[table_name]["Keys"].append(key)

        # The table key shape gives us a way to find the object in O(1)
        # from the attributes returned by Dynamo.
        if not table_exists:
            self.table_keys[table_name] = list(key.keys())

        index = tuple(_value_of(k) for k in key.values())
        self.indexed_objects[table_name][index] = obj
        self.objects.add(obj)

    def extend(self, objects):
        for obj in objects:
            self.add(obj)

    def pop(self, table_name, item):
        objects = self.indexed_objects[table_name]
        keys = self.table_keys[table_name]
        index = tuple(_value_of(item[k]) for k in keys)
        obj = objects.get(index)
        self.objects.remove(obj)
        return obj

    @property
    def missing(self):
        return self.objects


class Engine:
    model = None

    def __init__(self, *, session=None, **config):
        self.client = bloop.client.Client(session=session)
        # Unique namespace so the type engine for multiple bloop Engines
        # won't have the same TypeDefinitions
        self.type_engine = declare.TypeEngine.unique()
        self.model = bloop.model.BaseModel(self)
        self.unbound_models = set()
        self.models = set()
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(config)

    def _dump(self, model, obj):
        """ Return a dict of the obj in DynamoDB format """
        try:
            return self.type_engine.dump(model, obj)
        except declare.DeclareException:
            if model in self.unbound_models:
                raise bloop.exceptions.UnboundModel("load", model, obj)
            else:
                raise ValueError(
                    "Failed to dump unknown model {}".format(model))

    def _instance(self, model):
        """ Return an instance of a given model """
        return self._load(model, {})

    def _load(self, model, value):
        try:
            return self.type_engine.load(model, value)
        except declare.DeclareException:
            if model in self.unbound_models:
                raise bloop.exceptions.UnboundModel("load", model, None)
            else:
                raise ValueError(
                    "Failed to load unknown model {}".format(model))

    def _update(self, obj, attrs, expected):
        """ Push values by dynamo_name into an object """
        for column in expected:
            value = attrs.get(column.dynamo_name, None)
            if value is not None:
                value = self._load(column.typedef, value)
            setattr(obj, column.model_name, value)

    def bind(self):
        """ Create tables for all models that have been registered """
        # create_table doesn't block until ACTIVE or validate.
        # It also doesn't throw when the table already exists, making it safe
        # to call multiple times for the same unbound model.
        for model in self.unbound_models:
            self.client.create_table(model)

        unverified = set(self.unbound_models)
        while unverified:
            model = unverified.pop()

            self.client.validate_table(model)
            # If the call above didn't throw, everything's good to go.

            self.type_engine.register(model)
            for column in model.Meta.columns:
                self.type_engine.register(column.typedef)
            self.type_engine.bind()
            # If nothing above threw, we can mark this model bound

            self.unbound_models.remove(model)
            self.models.add(model)

    def context(self, **config):
        """
        with engine.context(atomic=True, consistent=True) as atomic:
            atomic.load(obj)
            del obj.foo
            obj.bar += 1
            atomic.save(obj)
        """
        return EngineView(self, **config)

    def delete(self, objs, *, condition=None):
        for obj in _list_of(objs):
            item = {"TableName": obj.Meta.table_name,
                    "Key": _dump_key(self, obj)}
            renderer = bloop.condition.ConditionRenderer(self)

            item_condition = bloop.condition.Condition()
            if self.config["atomic"]:
                item_condition &= bloop.tracking.get_snapshot(obj, self)
            if condition:
                item_condition &= condition
            renderer.render(item_condition, "condition")
            item.update(renderer.rendered)

            self.client.delete_item(item)

            bloop.tracking.clear_snapshot(obj, self)
            bloop.tracking.set_synced(obj)

    def load(self, objs):
        """
        Populate objects from dynamodb, optionally using consistent reads.

        If any objects are not found, throws ObjectsNotFound with the attribute
        `missing` containing a list of the objects that were not loaded.

        Example
        -------
        engine = Engine()

        class HashOnly(engine.model):
            user_id = Column(NumberType, hash_key=True)

        class HashAndRange(engine.model):
            user_id = Column(NumberType, hash_key=True)
            game_title = Column(StringType, range_key=True)

        hash_only = HashOnly(user_id=101)
        hash_and_range = HashAndRange(user_id=101, game_title="Starship X")

        # Load only one instance, with consistent reads
        with engine.context(consistent=True) as e:
            e.load(hash_only)

        # Load multiple instances
        engine.load(hash_only, hash_and_range)
        """
        request = LoadRequest(self)
        request.extend(_list_of(objs))
        results = self.client.batch_get_items(request.wire)

        for table_name, items in results.items():
            for item in items:
                obj = request.pop(table_name, item)
                self._update(obj, item, obj.Meta.columns)

                bloop.tracking.set_snapshot(obj, self)
                bloop.tracking.set_synced(obj)

        if request.missing:
            raise bloop.exceptions.NotModified("load", request.missing)

    def query(self, obj):
        if isinstance(obj, bloop.index._Index):
            model, index = obj.model, obj
        else:
            model, index = obj, None
        return bloop.filter.Query(engine=self, model=model, index=index)

    def save(self, objs, *, condition=None):
        for obj in _list_of(objs):
            item = {"TableName": obj.Meta.table_name,
                    "Key": _dump_key(self, obj)}
            renderer = bloop.condition.ConditionRenderer(self)

            diff = bloop.tracking.dump_update(obj)
            renderer.update(diff)

            item_condition = bloop.condition.Condition()
            if self.config["atomic"]:
                item_condition &= bloop.tracking.get_snapshot(obj, self)
            if condition:
                item_condition &= condition
            renderer.render(item_condition, "condition")
            item.update(renderer.rendered)

            self.client.update_item(item)

            bloop.tracking.set_snapshot(obj, self)
            bloop.tracking.set_synced(obj)

    def scan(self, obj):
        if isinstance(obj, bloop.index._Index):
                model, index = obj.model, obj
        else:
            model, index = obj, None
        return bloop.filter.Scan(engine=self, model=model, index=index)


class EngineView(Engine):
    def __init__(self, engine, **config):
        self.client = engine.client
        self.config = dict(engine.config)
        self.config.update(config)
        self.model = engine.model
        self.type_engine = engine.type_engine

    def bind(self):
        raise RuntimeError("EngineViews can't modify engine types or bindings")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
