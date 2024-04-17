"""
JSON serialization and deserialization utilities.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import traceback
import types
from collections import OrderedDict, defaultdict
from enum import Enum
from hashlib import sha1
from importlib import import_module
from inspect import getfullargspec
from pathlib import Path
from uuid import UUID

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

try:
    import pydantic
except ImportError:
    pydantic = None  # type: ignore

try:
    from pydantic_core import core_schema
except ImportError:
    core_schema = None  # type: ignore

try:
    import bson
except ImportError:
    bson = None  # type: ignore

try:
    from ruamel.yaml import YAML
except ImportError:
    YAML = None  # type: ignore

try:
    import orjson
except ImportError:
    orjson = None  # type: ignore

try:
    import dataclasses
except ImportError:
    dataclasses = None  # type: ignore

try:
    import torch
except ImportError:
    torch = None  # type: ignore

__version__ = "3.0.0"


def _load_redirect(redirect_file):
    try:
        with open(redirect_file) as f:
            yaml = YAML()
            d = yaml.load(f)
    except OSError:
        # If we can't find the file
        # Just use an empty redirect dict
        return {}

    # Convert the full paths to module/class
    redirect_dict = defaultdict(dict)
    for old_path, new_path in d.items():
        old_class = old_path.split(".")[-1]
        old_module = ".".join(old_path.split(".")[:-1])

        new_class = new_path.split(".")[-1]
        new_module = ".".join(new_path.split(".")[:-1])

        redirect_dict[old_module][old_class] = {
            "@module": new_module,
            "@class": new_class,
        }

    return dict(redirect_dict)


def _check_type(obj, type_str) -> bool:
    """Alternative to isinstance that avoids imports.

    Checks whether obj is an instance of the type defined by type_str. This
    removes the need to explicitly import type_str. Handles subclasses like
    isinstance does. E.g.::
        class A: pass
        class B(A): pass
        a, b = A(), B()
        assert isinstance(a, A)
        assert isinstance(b, B)
        assert isinstance(b, A)
        assert not isinstance(a, B)

    type_str: str | tuple[str]

    Note for future developers: the type_str is not always obvious for an
    object. For example, pandas.DataFrame is actually pandas.core.frame.DataFrame.
    To find out the type_str for an object, run type(obj).mro(). This will
    list all the types that an object can resolve to in order of generality
    (all objects have the builtins.object as the last one).
    """
    type_str = type_str if isinstance(type_str, tuple) else (type_str,)
    # I believe this try-except is only necessary for callable types
    try:
        mro = type(obj).mro()
    except TypeError:
        return False
    return any(o.__module__ + "." + o.__name__ == ts for o in mro for ts in type_str)


class MSONable:
    """
    This is a mix-in base class specifying an API for msonable objects. MSON
    is Monty JSON. Essentially, MSONable objects must implement an as_dict
    method, which must return a json serializable dict and must also support
    no arguments (though optional arguments to finetune the output is ok),
    and a from_dict class method that regenerates the object from the dict
    generated by the as_dict method. The as_dict method should contain the
    "@module" and "@class" keys which will allow the MontyEncoder to
    dynamically deserialize the class. E.g.::

        d["@module"] = self.__class__.__module__
        d["@class"] = self.__class__.__name__

    A default implementation is provided in MSONable, which automatically
    determines if the class already contains self.argname or self._argname
    attributes for every arg. If so, these will be used for serialization in
    the dict format. Similarly, the default from_dict will deserialization
    classes of such form. An example is given below::

        class MSONClass(MSONable):

        def __init__(self, a, b, c, d=1, **kwargs):
            self.a = a
            self.b = b
            self._c = c
            self._d = d
            self.kwargs = kwargs

    For such classes, you merely need to inherit from MSONable and you do not
    need to implement your own as_dict or from_dict protocol.

    New to Monty V2.0.6....
    Classes can be redirected to moved implementations by putting in the old
    fully qualified path and new fully qualified path into .monty.yaml in the
    home folder

    Example:
    old_module.old_class: new_module.new_class
    """

    REDIRECT = _load_redirect(os.path.join(os.path.expanduser("~"), ".monty.yaml"))

    def as_dict(self) -> dict:
        """
        A JSON serializable dict representation of an object.
        """
        d = {"@module": self.__class__.__module__, "@class": self.__class__.__name__}

        try:
            parent_module = self.__class__.__module__.split(".", maxsplit=1)[0]
            module_version = import_module(parent_module).__version__  # type: ignore
            d["@version"] = str(module_version)
        except (AttributeError, ImportError):
            d["@version"] = None  # type: ignore

        spec = getfullargspec(self.__class__.__init__)

        def recursive_as_dict(obj):
            if isinstance(obj, (list, tuple)):
                return [recursive_as_dict(it) for it in obj]
            if isinstance(obj, dict):
                return {kk: recursive_as_dict(vv) for kk, vv in obj.items()}
            if hasattr(obj, "as_dict"):
                return obj.as_dict()
            if dataclasses is not None and dataclasses.is_dataclass(obj):
                d = dataclasses.asdict(obj)
                d.update(
                    {
                        "@module": obj.__class__.__module__,
                        "@class": obj.__class__.__name__,
                    }
                )
                return d
            return obj

        for c in spec.args + spec.kwonlyargs:
            if c != "self":
                try:
                    a = getattr(self, c)
                except AttributeError:
                    try:
                        a = getattr(self, "_" + c)
                    except AttributeError:
                        raise NotImplementedError(
                            "Unable to automatically determine as_dict "
                            "format from class. MSONAble requires all "
                            "args to be present as either self.argname or "
                            "self._argname, and kwargs to be present under "
                            "a self.kwargs variable to automatically "
                            "determine the dict format. Alternatively, "
                            "you can implement both as_dict and from_dict."
                        )
                d[c] = recursive_as_dict(a)
        if hasattr(self, "kwargs"):
            # type: ignore
            d.update(**self.kwargs)  # pylint: disable=E1101
        if spec.varargs is not None and getattr(self, spec.varargs, None) is not None:
            d.update({spec.varargs: getattr(self, spec.varargs)})
        if hasattr(self, "_kwargs"):
            d.update(**self._kwargs)  # pylint: disable=E1101
        if isinstance(self, Enum):
            d.update({"value": self.value})  # pylint: disable=E1101
        return d

    @classmethod
    def from_dict(cls, d):
        """
        :param d: Dict representation.
        :return: MSONable class.
        """
        decoded = {
            k: MontyDecoder().process_decoded(v)
            for k, v in d.items()
            if not k.startswith("@")
        }
        return cls(**decoded)

    def to_json(self) -> str:
        """
        Returns a json string representation of the MSONable object.
        """
        return json.dumps(self, cls=MontyEncoder)

    def unsafe_hash(self):
        """
        Returns an hash of the current object. This uses a generic but low
        performance method of converting the object to a dictionary, flattening
        any nested keys, and then performing a hash on the resulting object
        """

        def flatten(obj, separator="."):
            # Flattens a dictionary

            flat_dict = {}
            for key, value in obj.items():
                if isinstance(value, dict):
                    flat_dict.update(
                        {
                            separator.join([key, _key]): _value
                            for _key, _value in flatten(value).items()
                        }
                    )
                elif isinstance(value, list):
                    list_dict = {
                        f"{key}{separator}{num}": item for num, item in enumerate(value)
                    }
                    flat_dict.update(flatten(list_dict))
                else:
                    flat_dict[key] = value

            return flat_dict

        ordered_keys = sorted(
            flatten(jsanitize(self.as_dict())).items(), key=lambda x: x[0]
        )
        ordered_keys = [item for item in ordered_keys if "@" not in item[0]]
        return sha1(json.dumps(OrderedDict(ordered_keys)).encode("utf-8"))

    @classmethod
    def _validate_monty(cls, __input_value):
        """
        pydantic Validator for MSONable pattern
        """
        if isinstance(__input_value, cls):
            return __input_value
        if isinstance(__input_value, dict):
            # Do not allow generic exceptions to be raised during deserialization
            # since pydantic may handle them incorrectly.
            try:
                new_obj = MontyDecoder().process_decoded(__input_value)
                if isinstance(new_obj, cls):
                    return new_obj
                return cls(**__input_value)
            except Exception:
                raise ValueError(
                    f"Error while deserializing {cls.__name__} "
                    f"object: {traceback.format_exc()}"
                )

        raise ValueError(
            f"Must provide {cls.__name__}, the as_dict form, or the proper"
        )

    @classmethod
    def validate_monty_v1(cls, __input_value):
        """
        Pydantic validator with correct signature for pydantic v1.x
        """
        return cls._validate_monty(__input_value)

    @classmethod
    def validate_monty_v2(cls, __input_value, _):
        """
        Pydantic validator with correct signature for pydantic v2.x
        """
        return cls._validate_monty(__input_value)

    @classmethod
    def __get_validators__(cls):
        """Return validators for use in pydantic"""
        yield cls.validate_monty_v1

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        """
        pydantic v2 core schema definition
        """
        if core_schema is None:
            raise RuntimeError("Pydantic >= 2.0 is required for validation")

        s = core_schema.with_info_plain_validator_function(cls.validate_monty_v2)

        return core_schema.json_or_python_schema(json_schema=s, python_schema=s)

    @classmethod
    def _generic_json_schema(cls):
        return {
            "type": "object",
            "properties": {
                "@class": {"enum": [cls.__name__], "type": "string"},
                "@module": {"enum": [cls.__module__], "type": "string"},
                "@version": {"type": "string"},
            },
            "required": ["@class", "@module"],
        }

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """JSON schema for MSONable pattern"""
        return cls._generic_json_schema()

    @classmethod
    def __modify_schema__(cls, field_schema):
        """JSON schema for MSONable pattern"""
        custom_schema = cls._generic_json_schema()
        field_schema.update(custom_schema)


class MontyEncoder(json.JSONEncoder):
    """
    A Json Encoder which supports the MSONable API, plus adds support for
    numpy arrays, datetime objects, bson ObjectIds (requires bson).
    Usage::
        # Add it as a *cls* keyword when using json.dump
        json.dumps(object, cls=MontyEncoder)
    """

    def default(self, o) -> dict:  # pylint: disable=E0202
        """
        Overriding default method for JSON encoding. This method does two
        things: (a) If an object has a to_dict property, return the to_dict
        output. (b) If the @module and @class keys are not in the to_dict,
        add them to the output automatically. If the object has no to_dict
        property, the default Python json encoder default method is called.
        Args:
            o: Python object.
        Return:
            Python dict representation.
        """
        if isinstance(o, datetime.datetime):
            return {"@module": "datetime", "@class": "datetime", "string": str(o)}
        if isinstance(o, UUID):
            return {"@module": "uuid", "@class": "UUID", "string": str(o)}
        if isinstance(o, Path):
            return {"@module": "pathlib", "@class": "Path", "string": str(o)}

        if torch is not None and isinstance(o, torch.Tensor):
            # Support for Pytorch Tensors.
            d = {
                "@module": "torch",
                "@class": "Tensor",
                "dtype": o.type(),
            }
            if "Complex" in o.type():
                d["data"] = [o.real.tolist(), o.imag.tolist()]  # type: ignore
            else:
                d["data"] = o.numpy().tolist()
            return d

        if np is not None:
            if isinstance(o, np.ndarray):
                if str(o.dtype).startswith("complex"):
                    return {
                        "@module": "numpy",
                        "@class": "array",
                        "dtype": str(o.dtype),
                        "data": [o.real.tolist(), o.imag.tolist()],
                    }
                return {
                    "@module": "numpy",
                    "@class": "array",
                    "dtype": str(o.dtype),
                    "data": o.tolist(),
                }
            if isinstance(o, np.generic):
                return o.item()

        if _check_type(o, "pandas.core.frame.DataFrame"):
            return {
                "@module": "pandas",
                "@class": "DataFrame",
                "data": o.to_json(default_handler=MontyEncoder().encode),
            }
        if _check_type(o, "pandas.core.series.Series"):
            return {
                "@module": "pandas",
                "@class": "Series",
                "data": o.to_json(default_handler=MontyEncoder().encode),
            }

        if bson is not None and isinstance(o, bson.objectid.ObjectId):
            return {"@module": "bson.objectid", "@class": "ObjectId", "oid": str(o)}

        if callable(o) and not isinstance(o, MSONable):
            return _serialize_callable(o)

        try:
            if pydantic is not None and isinstance(o, pydantic.BaseModel):
                d = o.dict()
            elif (
                dataclasses is not None
                and (not issubclass(o.__class__, MSONable))
                and dataclasses.is_dataclass(o)
            ):
                # This handles dataclasses that are not subclasses of MSONAble.
                d = dataclasses.asdict(o)
            elif hasattr(o, "as_dict"):
                d = o.as_dict()
            elif isinstance(o, Enum):
                d = {"value": o.value}
            else:
                raise TypeError(
                    f"Object of type {o.__class__.__name__} is not JSON serializable"
                )

            if "@module" not in d:
                d["@module"] = str(o.__class__.__module__)
            if "@class" not in d:
                d["@class"] = str(o.__class__.__name__)
            if "@version" not in d:
                try:
                    parent_module = o.__class__.__module__.split(".")[0]
                    module_version = import_module(parent_module).__version__  # type: ignore
                    d["@version"] = str(module_version)
                except (AttributeError, ImportError):
                    d["@version"] = None  # type: ignore
            return d
        except AttributeError:
            return json.JSONEncoder.default(self, o)


class MontyDecoder(json.JSONDecoder):
    """
    A Json Decoder which supports the MSONable API. By default, the
    decoder attempts to find a module and name associated with a dict. If
    found, the decoder will generate a Pymatgen as a priority.  If that fails,
    the original decoded dictionary from the string is returned. Note that
    nested lists and dicts containing pymatgen object will be decoded correctly
    as well.

    Usage:

        # Add it as a *cls* keyword when using json.load
        json.loads(json_string, cls=MontyDecoder)
    """

    def process_decoded(self, d):
        """
        Recursive method to support decoding dicts and lists containing
        pymatgen objects.
        """
        if isinstance(d, dict):
            if "@module" in d and "@class" in d:
                modname = d["@module"]
                classname = d["@class"]
                if cls_redirect := MSONable.REDIRECT.get(modname, {}).get(classname):
                    classname = cls_redirect["@class"]
                    modname = cls_redirect["@module"]
            elif "@module" in d and "@callable" in d:
                modname = d["@module"]
                objname = d["@callable"]
                classname = None
                if d.get("@bound", None) is not None:
                    # if the function is bound to an instance or class, first
                    # deserialize the bound object and then remove the object name
                    # from the function name.
                    obj = self.process_decoded(d["@bound"])
                    objname = objname.split(".")[1:]
                else:
                    # if the function is not bound to an object, import the
                    # function from the module name
                    obj = __import__(modname, globals(), locals(), [objname], 0)
                    objname = objname.split(".")
                try:
                    # the function could be nested. e.g., MyClass.NestedClass.function
                    # so iteratively access the nesting
                    for attr in objname:
                        obj = getattr(obj, attr)

                    return obj

                except AttributeError:
                    pass
            else:
                modname = None
                classname = None

            if classname:
                if modname and modname not in [
                    "bson.objectid",
                    "numpy",
                    "pandas",
                    "torch",
                ]:
                    if modname == "datetime" and classname == "datetime":
                        try:
                            dt = datetime.datetime.strptime(
                                d["string"], "%Y-%m-%d %H:%M:%S.%f"
                            )
                        except ValueError:
                            dt = datetime.datetime.strptime(
                                d["string"], "%Y-%m-%d %H:%M:%S"
                            )
                        return dt

                    if modname == "uuid" and classname == "UUID":
                        return UUID(d["string"])

                    if modname == "pathlib" and classname == "Path":
                        return Path(d["string"])

                    mod = __import__(modname, globals(), locals(), [classname], 0)
                    if hasattr(mod, classname):
                        cls_ = getattr(mod, classname)
                        data = {k: v for k, v in d.items() if not k.startswith("@")}
                        if hasattr(cls_, "from_dict"):
                            return cls_.from_dict(data)
                        if issubclass(cls_, Enum):
                            return cls_(d["value"])
                        if pydantic is not None and issubclass(
                            cls_, pydantic.BaseModel
                        ):  # pylint: disable=E1101
                            d = {k: self.process_decoded(v) for k, v in data.items()}
                            return cls_(**d)
                        if (
                            dataclasses is not None
                            and (not issubclass(cls_, MSONable))
                            and dataclasses.is_dataclass(cls_)
                        ):
                            d = {k: self.process_decoded(v) for k, v in data.items()}
                            return cls_(**d)
                elif torch is not None and modname == "torch" and classname == "Tensor":
                    if "Complex" in d["dtype"]:
                        return torch.tensor(  # pylint: disable=E1101
                            [
                                np.array(r) + np.array(i) * 1j
                                for r, i in zip(*d["data"])
                            ],
                        ).type(d["dtype"])
                    return torch.tensor(d["data"]).type(d["dtype"])  # pylint: disable=E1101
                elif np is not None and modname == "numpy" and classname == "array":
                    if d["dtype"].startswith("complex"):
                        return np.array(
                            [
                                np.array(r) + np.array(i) * 1j
                                for r, i in zip(*d["data"])
                            ],
                            dtype=d["dtype"],
                        )
                    return np.array(d["data"], dtype=d["dtype"])
                elif modname == "pandas":
                    import pandas as pd

                    if classname == "DataFrame":
                        decoded_data = MontyDecoder().decode(d["data"])
                        return pd.DataFrame(decoded_data)
                    if classname == "Series":
                        decoded_data = MontyDecoder().decode(d["data"])
                        return pd.Series(decoded_data)
                elif (
                    (bson is not None)
                    and modname == "bson.objectid"
                    and classname == "ObjectId"
                ):
                    return bson.objectid.ObjectId(d["oid"])

            return {
                self.process_decoded(k): self.process_decoded(v) for k, v in d.items()
            }

        if isinstance(d, list):
            return [self.process_decoded(x) for x in d]

        return d

    def decode(self, s):
        """
        Overrides decode from JSONDecoder.

        :param s: string
        :return: Object.
        """
        if orjson is not None:
            try:
                d = orjson.loads(s)  # pylint: disable=E1101
            except orjson.JSONDecodeError:  # pylint: disable=E1101
                d = json.loads(s)
        else:
            d = json.loads(s)
        return self.process_decoded(d)


class MSONError(Exception):
    """
    Exception class for serialization errors.
    """


def jsanitize(
    obj, strict=False, allow_bson=False, enum_values=False, recursive_msonable=False
):
    """
    This method cleans an input json-like object, either a list or a dict or
    some sequence, nested or otherwise, by converting all non-string
    dictionary keys (such as int and float) to strings, and also recursively
    encodes all objects using Monty's as_dict() protocol.

    Args:
        obj: input json-like object.
        strict (bool): This parameter sets the behavior when jsanitize
            encounters an object it does not understand. If strict is True,
            jsanitize will try to get the as_dict() attribute of the object. If
            no such attribute is found, an attribute error will be thrown. If
            strict is False, jsanitize will simply call str(object) to convert
            the object to a string representation.
        allow_bson (bool): This parameter sets the behavior when jsanitize
            encounters a bson supported type such as objectid and datetime. If
            True, such bson types will be ignored, allowing for proper
            insertion into MongoDB databases.
        enum_values (bool): Convert Enums to their values.
        recursive_msonable (bool): If True, uses .as_dict() for MSONables regardless
            of the value of strict.

    Returns:
        Sanitized dict that can be json serialized.
    """
    if isinstance(obj, Enum):
        if enum_values:
            return obj.value
        elif hasattr(obj, "as_dict"):
            return obj.as_dict()
        return MontyEncoder().default(obj)

    if allow_bson and (
        isinstance(obj, (datetime.datetime, bytes))
        or (bson is not None and isinstance(obj, bson.objectid.ObjectId))
    ):
        return obj
    if isinstance(obj, (list, tuple)):
        return [
            jsanitize(i, strict=strict, allow_bson=allow_bson, enum_values=enum_values, recursive_msonable=recursive_msonable)
            for i in obj
        ]
    if np is not None and isinstance(obj, np.ndarray):
        return [
            jsanitize(i, strict=strict, allow_bson=allow_bson, enum_values=enum_values, recursive_msonable=recursive_msonable)
            for i in obj.tolist()
        ]
    if np is not None and isinstance(obj, np.generic):
        return obj.item()
    if _check_type(
        obj,
        (
            "pandas.core.series.Series",
            "pandas.core.frame.DataFrame",
            "pandas.core.base.PandasObject",
        ),
    ):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {
            str(k): jsanitize(
                v,
                strict=strict,
                allow_bson=allow_bson,
                enum_values=enum_values,
                recursive_msonable=recursive_msonable,
            )
            for k, v in obj.items()
        }
    if isinstance(obj, (int, float)):
        return obj
    if obj is None:
        return None
    if isinstance(obj, (pathlib.Path, datetime.datetime)):
        return str(obj)

    if callable(obj) and not isinstance(obj, MSONable):
        try:
            return _serialize_callable(obj)
        except TypeError:
            pass

    if recursive_msonable:
        try:
            return obj.as_dict()
        except AttributeError:
            pass

    if not strict:
        return str(obj)

    if isinstance(obj, str):
        return obj

    if pydantic is not None and isinstance(obj, pydantic.BaseModel):  # pylint: disable=E1101
        return jsanitize(
            MontyEncoder().default(obj),
            strict=strict,
            allow_bson=allow_bson,
            enum_values=enum_values,
            recursive_msonable=recursive_msonable,
        )

    return jsanitize(
        obj.as_dict(),
        strict=strict,
        allow_bson=allow_bson,
        enum_values=enum_values,
        recursive_msonable=recursive_msonable,
    )


def _serialize_callable(o):
    if isinstance(o, types.BuiltinFunctionType):
        # don't care about what builtin functions (sum, open, etc) are bound to
        bound = None
    else:
        # bound methods (i.e., instance methods) have a __self__ attribute
        # that points to the class/module/instance
        bound = getattr(o, "__self__", None)

    # we are only able to serialize bound methods if the object the method is
    # bound to is itself serializable
    if bound is not None:
        try:
            bound = MontyEncoder().default(bound)
        except TypeError:
            raise TypeError(
                "Only bound methods of classes or MSONable instances are supported."
            )

    return {
        "@module": o.__module__,
        "@callable": getattr(o, "__qualname__", o.__name__),
        "@bound": bound,
    }
