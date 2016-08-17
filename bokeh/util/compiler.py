from __future__ import absolute_import

import logging
logger = logging.getLogger(__name__)

import six
import json
import inspect
import hashlib
from os.path import basename, dirname, join, abspath, relpath, exists
from subprocess import Popen, PIPE

from ..model import Model
from ..settings import settings
from .string import snakify
from .paths import bokehjsdir

# XXX: this is the same as bokehjs/src/js/plugin-prelude.js
_plugin_prelude = \
"""
(function outer(modules, cache, entry) {
  if (typeof Bokeh !== "undefined") {
    for (var name in modules) {
      Bokeh.require.modules[name] = modules[name];
    }

    for (var i = 0; i < entry.length; i++) {
        Bokeh.Models.register_locations(Bokeh.require(entry[i]));
    }
  } else {
    throw new Error("Cannot find Bokeh. You have to load it prior to loading plugins.");
  }
})
"""

_plugin_template = \
"""
%(prelude)s
({
  "custom/main": [function(require, module, exports) {
    module.exports = { %(exports)s };
  }, {}],
  %(modules)s
}, {}, ["custom/main"]);
"""

_style_template = \
"""
(function() {
  var head = document.getElementsByTagName('head')[0];
  var style = document.createElement('style');
  style.type = 'text/css';
  var css = %(css)s;
  if (style.styleSheet) {
    style.styleSheet.cssText = css;
  } else {
    style.appendChild(document.createTextNode(css));
  }
  head.appendChild(style);
}());
"""

_export_template = \
"""%(name)s: require("%(module)s")"""

_module_template = \
""""%(module)s": [function(require, module, exports) {\n%(code)s\n}, %(deps)s]"""

class AttrDict(dict):
    def __getattr__(self, key):
        return self[key]

class CompilationError(RuntimeError):

    def __init__(self, error):
        super(CompilationError, self).__init__()
        self.line = error.get("line")
        self.column = error.get("column")
        self.message = error.get("message")
        self.text = error.get("text")
        self.annotated = error.get("annotated")

    def __str__(self):
        return self.text

bokehjs_dir = settings.bokehjsdir()

def _detect_nodejs():
    if settings.nodejs_path() is not None:
        nodejs_paths = [settings.nodejs_path()]
    else:
        nodejs_paths = ["xnodejs", "node"]

    for nodejs_path in nodejs_paths:
        try:
            proc = Popen([nodejs_path, "--version"], stdout=PIPE, stderr=PIPE)
        except OSError:
            pass
        else:
            return nodejs_path
    else:
        return None

_nodejs = _detect_nodejs()

def _run_nodejs(script, input):
    if _nodejs is None:
        raise RuntimeError('node.js is needed to allow compilation of custom models ' +
                           '("conda install -c bokeh nodejs" or follow https://nodejs.org/en/download/)')

    proc = Popen([_nodejs, script], stdout=PIPE, stderr=PIPE, stdin=PIPE)
    (stdout, errout) = proc.communicate(input=json.dumps(input).encode())

    if len(errout) > 0:
        raise RuntimeError(errout)
    else:
        return AttrDict(json.loads(stdout.decode()))

def nodejs_compile(code, lang="javascript", file=None):
    compilejs_script = join(bokehjs_dir, "js", "compile.js")
    return _run_nodejs(compilejs_script, dict(code=code, lang=lang, file=file))

class Implementation(object):
    pass

class Inline(Implementation):

    def __init__(self, code, file=None):
        self.code = code
        self.file = file

class CoffeeScript(Inline):

    @property
    def lang(self):
        return "coffeescript"

class JavaScript(Inline):

    @property
    def lang(self):
        return "javascript"

class Less(Inline):

    @property
    def lang(self):
        return "less"

class FromFile(Implementation):

    def __init__(self, path):
        with open(path, "rb") as f:
            self.code = f.read()
        self.file = path

    @property
    def lang(self):
        if self.file.endswith(".coffee"):
            return "coffeescript"
        if self.file.endswith(".js"):
            return "javascript"
        if self.file.endswith((".css", ".less")):
            return "less"

class CustomModel(object):
    def __init__(self, cls):
        self.cls = cls

    @property
    def name(self):
        return self.cls.__name__

    @property
    def full_name(self):
        name = self.cls.__module__ + "." + self.name
        return name.replace("__main__.", "")

    @property
    def file(self):
        return abspath(inspect.getfile(self.cls))

    @property
    def path(self):
        return dirname(self.file)

    @property
    def implementation(self):
        impl = self.cls.__implementation__

        if isinstance(impl, six.string_types):
            if "\n" not in impl and impl.endswith((".coffee", ".js", ".css", ".less")):
                impl = FromFile(join(self.path, impl))
            else:
                impl = CoffeeScript(impl)

        if isinstance(impl, Inline) and impl.file is None:
            impl = impl.__class__(impl.code, self.file + ":" + self.name)

        return impl

    @property
    def module(self):
        return "custom/%s" % snakify(self.full_name)

def gen_custom_models_static():
    custom_models = {}

    for cls in Model.model_class_reverse_map.values():
        impl = getattr(cls, "__implementation__", None)

        if impl is not None:
            model = CustomModel(cls)
            custom_models[model.full_name] = model

    if not custom_models:
        return None

    exports = []
    modules = []

    with open(join(bokehjs_dir, "js", "modules.json")) as f:
        known_modules = json.loads(f.read())

    known_modules = set(known_modules["bokehjs"] + known_modules["widgets"])

    custom_impls = {}

    for model in sorted(custom_models.values(), key=lambda model: model.full_name):
        impl = model.implementation
        compiled = nodejs_compile(impl.code, lang=impl.lang, file=impl.file)

        if "error" in compiled:
            raise CompilationError(compiled.error)

        custom_impls[model.full_name] = compiled

    extra_modules = {}

    def resolve_modules(to_resolve, root):
        resolved = {}
        for module in to_resolve:
            if module.startswith(("./", "../")):
                def mkpath(ext):
                    return abspath(join(root, *module.split("/")) + "." + ext)

                for ext in ["js", "coffee", "css", "less"]:
                    path = mkpath(ext)
                    if exists(path):
                        break
                else:
                    raise RuntimeError("no such module: %s" % module)

                impl = FromFile(path)
                compiled = nodejs_compile(impl.code, lang=impl.lang, file=impl.file)

                if "error" in compiled:
                    raise CompilationError(compiled.error)

                if impl.lang == "less":
                    code = _style_template % dict(css=json.dumps(compiled.code))
                    deps = []
                else:
                    code = compiled.code
                    deps = compiled.deps

                sig = hashlib.sha256(code).hexdigest()
                resolved[module] = sig

                deps_map = resolve_deps(deps, dirname(path))

                if sig not in extra_modules:
                    extra_modules[sig] = True
                    modules.append((sig, code, deps_map))
            else:
                raise RuntimeError("no such module: %s" % module)

        return resolved

    def resolve_deps(deps, root):
        custom_modules = set([ model.module for model in custom_models.values() ])
        missing = set(deps) - known_modules - custom_modules
        return resolve_modules(missing, root)

    for model in custom_models.values():
        compiled = custom_impls[model.full_name]
        deps_map = resolve_deps(compiled.deps, model.path)

        exports.append((model.name, model.module))
        modules.append((model.module, compiled.code, deps_map))

    # sort everything by module name
    exports = sorted(exports, key=lambda spec: spec[1])
    modules = sorted(modules, key=lambda spec: spec[0])

    sep = ",\n"

    exports = sep.join([ _export_template % dict(name=name, module=module) for (name, module) in exports ])
    modules = sep.join([ _module_template % dict(module=module, code=code, deps=json.dumps(deps)) for (module, code, deps) in modules ])

    return _plugin_template % dict(prelude=_plugin_prelude, exports=exports, modules=modules)
