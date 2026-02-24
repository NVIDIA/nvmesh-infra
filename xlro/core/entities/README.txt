# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

An entity is a python class modeling the state of a domain object, e.g., Client, Drive, Volume, PRaid.
Instances have several common properties that differentiate them from regular objects:
    * Unique key: For any given entity, only one instance per key will be allowed at any one time. For example,
    Volume(name='joe') will create an instance of Volume object with name 'joe'. An attempt to create another instance
    with the same name will fail. Volume.instance(name='joe') will return the existing 'joe' Volume or create one if
    required. If additional non-key arguments are given to .instance(), those properties will be set on the created
    or found object.
    * Declared schema of properties: Each entity declares its properties and their associated types and other
    meta-info (for example if the property is part of the key).  The values for properties (other then key properties)
    are loaded lazily, when referenced, and, by default cached. Thus they represent a snapshot of state.
    A given property can have multiple values depending on 'view' (below). Properties can be primitives, references
    to other Entities, or collections (dict/list) of the above.
    (The declared schema is not limiting. New, multi-valued properties can be discoverd during loading.)
    * Views/Sources: Views are a set of property values of an entity based on a given Source. For example, it is
    possible for a value for a Volume to differ between what the Management source thinks vs. what is discovered from a
    /proc source on an attached Client. This may be due to a bug, or a natural eventual consistency in a distributed
    environment. These views are represented by a different property-value set on the SAME uniquely keyed instance.
    (Eventually, properties might support a time series of values.)

BaseEntity is the root base-class of all entities. It provides common entity functions to support the above aspects,
such as:
- get/set properties (with support for different values per source-type)
- serialize/deserialize to/from dict
- lazy loading of values via loaders framework
- key/instance mgmt
- global type registry and instance cache

An entity class declares its properties using PropertySpec(), which declared their type and attributes (such as key,
transient, etc.). It entity also declares a view/source priority, with the first source being the default source for
that entity. The class should also decleare loader methods per source and property or set of properties, The loader
methods implemenent the lazy loading, and are triggered by the base Entity class when a property value is not found
(or cache is explicitly ignored).
The class can also declare operational methods (e.g., Client.attach(volume)) or anything else as a normal Python class.

In addition to inheriting from BaseEntity (or an intermediate class) the @entity and @loader decorators are required
to create the metadata from the class/property declaration, e.g., the list of key fields. etc.

For example:

@entity([SourceTypes.PROC, SourceTypes.MGMT])
class Widget(BaseEntity):
    name = PropertySpec(str, key=True)
    height = PropertySpec(int)
    width = PropertySpec(int)
    color = PropertySpec(str)
    parts = PropertySpec(['Widget'])

    @prop_loader(SourceTypes.PROC, None)
    def proc_loader(self):
        # type: () -> Dict[str, Any]
        with open(os.join('/proc/widgets', self.name)) as fp:
             return json.load(fp)

# Create or locate the existing Widget named 'widget-1'
w = Widget.instance(name='widget-1')
# Return the color value. If it wasn't already loaded, the loader(s) will be triggered until it's found or KeyError
print 'Widget {} is of color: {}'.format(w.name, w.color)

The BaseEntity contains the implementation for property access, which is the bulk of the complexity of an Entity.
It relies on the metadata of the Entity class, which is initialized by the decorators. The general flow is as follows:

0. Decorators gather metadata (import time):
    The @entity class decorator scans the class definition for PropertySpec()'s which define the 'schema' of the
    entity. The PropertySpec defines the type of the property, an optional default value, and some boolean properties
    such as is_a_key, is_transient, etc. It builds seveal meta properties on the class, including:
    _xlro_props: a dict mapping property name to the full PropertySpec
    _xlro_keyprops: a list of the key property names (convenience, derived from _xlro_props)
    _xlro_defaults: a dict mapping property name to default value (convenience, derived from _xlro_props)
    _xlro_loaders: a nested dict of loader methods, as explained below...

    The @prop_loader method decorator declares a method as a loader. As described above, the property values for an
    entity are lazily loaded, and stored per source view. This is implemented via loader methods. A loader method
    uses the decorator to declare what properties it can load from what source. For example, a method decorated as:
    @prop_loader(SourceTypes.PROC, ['type', 'blocks', 'status']) loads values for the 3 named properties from the PROC
    source. An empty list means a loader may be able to load ANY property.
    Each loader method must return a dictionary mapping property names to values. (It is not possible that a loader
    may not actually have a value, even for a property it declared it was capable of loading. This is not necessarily
    an error and is handled in the base entity.)

    The _xlro_loaders is the result of the @entity class decorator scanning for all the loader methods and building
    a dict which, per source, maps the property names to their loader functions. This is then consulted when a
    property get() is triggered. So the key of the top-level dict is Source, and that maps to inner dicts per source
    which map property name to loader methods. There can be a special key in the per-source dict of '*', which maps
    to the loaders that declared they can load ANY properties.

    In addition to metadata per entity class, the BaseEntity maintains a global mapping: BaseEntity.ENTITY_REGISTRY
    which maps the base name of the Entity class to the Class definition itself. All known entity types are stored
    here, regardless of whether instances exist or not.

1. Instances created and tracked:
    BaseEntity also maintains a mapping of created Entity instances in the global BaseEntity.ENTITY_CACHE. This
    cache maps a key to an instance. As mentioned in the definition of Entities, only a single instance should
    exist in memory for a given Key. (The type of the Entity is automatically prepended to all keys.)
    The __init__ method() builds a key using the internal _genkey() method, based on the passed keyword args,
    and attempts to insert an entry for the generated type/key into the ENTITY_CACHE. If an entry already exists,
    an Exception is raised.

    For this reason, almost all entity users should use the Entity.instance() method which takes the same arguments
    as __init__(), but if the entity exists, will return the existing object.

    There is, however, an important distinction between __init__() and instance(). The non-key arguments to __init__()
    are used to set other properties of the instance. Considering the widget above, 'name' is a required parameter
    to __init__() as it is a key field. 'color', however, is just another property. So:

    w1 = Widget(name='foo', color='blue')
    w2 = Widget.instance(name='bar', color='red')
    w3 = Widget.instance(name='foo', color='red')

    Only two objects will be created above, with keys 'Widget:name=foo' and 'Widget:name=bar'. w3 and w1 will
    reference the same object, and despite the argument on the 3rd line, the color will be 'blue'. This is because
    unlike the w2 instance() which created a new Widget, the w3 instance() found an existing widget. Once found vs.
    created, the other arguments are ignored.
    The current behavior was based on the understanding that the non-key values were a form of default settings,
    which was ignored if not needed. This behavior should be clarified and possibly changed. Note that:

    w4 = Widget.instance(name='bar', color='green', height=50)

    will result in w4 == w2 (by common key) and w4.color=='red' from w2 creation, but w2.height undefined!

2. Property Access
    The declared properties of an object behave as @property python properties, meaning accessing them as
    xyz.property will invoke a getter() method and assignment as xyz.property = 1 will invoke a setter() method.
    In our case the getter/setter is directed to get_property() and set_property() of BaseEntity.

    As part of the creation of an instance, a set of dicts will be created to track the per-source values.
    Each instance will have self._property_maps which is an ordered, nested dictionary. The top level key is Source,
    which maps to a dict of property name to value mappings. The ordering of the top-level is defined by the source
    passed to the @entity decorator.

    The entity property getter method is:
        def get_property(self, prop, source=None, no_cache=False): ...

    By default, e.g., when widget.color is used, self.get_property(prop='color') will be called. With no explicit
    source passed, each entry in self._property_maps will be checked for a contained 'color'. If nothing is found,
    the self._xlro_loaders nested dict will be similarly checked for a 'color' or general ('*') loader method.
    If the loader fails or returns a dict without a 'color' key, the next loader will be attempted. The first value
    found will be stored in the associated self._property_maps() entry and be returned.
    If no loaders return a color, KeyError will be raised.
    Note that as a side effect of the loading process above, OTHER key/values returned by general loaders could be
    stored in self.propert_maps for use by later getter calls.

    Users of the entity may call get_property() explicitly to override the default behavior.
    If an explicit source is passed to get_property() only values and loaders for that Source will be consulted.
    If no_cache is set to True, the initial check in the property_maps cache is skipped and the method tries the loaders
    in an attempt to get a new value.

    The entity property setter method is:
        def set_property(self, prop, value, source=None, refmap=None): ...

    By default, e.g., when widget.color = 'red' is used, the source will default to the first Source defined in
    the @entity decorator, resulting in self._property_maps[first_source]['color'] = 'red'.

    However, set_property() is a bit more sophisticated, because it automatically handles type conversion and most
    importantly, conversion of dictionaries to Entities. In a simple conversion, if a Color object was assigned above
    it would be converted as str(colorInstance) because the 'color' PropertySpec declared it as str.
    But consider the 'parts' property, defined as a list of Widgets.
    Primarily to support serialization and deserialization, and also to support loaders whose source format is JSON
    or other dictionaries (such as re.groupdict()), set_property() will convert a dict to an entity by using it's
    key/value mapping to set the properties of the instantiated instance.
    So widget.parts = [ {'name': 'sub1', 'color': 'red'}, {'name': 'sub2', 'color': 'blue', height: 50} ] will
    automatically do the "right" thing by converting the list to Widget entity references. This is recursive, so
    widget.parts = [ {'name': 'sub1', 'color': 'red', parts: [{'name': 'sub1.1'}, {'name': 'sub1.2'}}] ]
    will recursively create the sub-widgets of the widgets assigned to widget.parts.
