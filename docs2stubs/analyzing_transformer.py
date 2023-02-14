from collections import Counter
import inspect
import re
from types import ModuleType
from typing import Any, cast
import libcst as cst
import numpy as np
import pandas as pd
import scipy


from .base_transformer import BaseTransformer
from .utils import Sections, State, process_module, load_type_maps, save_fullmap, save_result, save_import_map, save_docstrings
from .docstring_parser import NumpyDocstringParser
from .traces import get_method_signature, get_toplevel_function_signature, init_trace_loader, simplify_types
from .type_normalizer import is_trivial, normalize_type, print_norm1


# Collection of all the docstrings, for use by the augmenter
_fullmap = Sections[dict[str, str|dict[str,str]]]({}, {}, {})

class AnalyzingTransformer(BaseTransformer):

    def __init__(self, 
            mod: ModuleType, 
            modname: str,
            fname: str, 
            state: State)-> None:
        """
        Params:
          mod - the module object. Used to get docstrings.
          modname - the module name.
          fname - the file name.
          state - the state object used for collecting analysis results for all modules
        """
        super().__init__(modname, fname)
        self._mod = mod
        self._parser = NumpyDocstringParser()

        self._docs: dict[str, Sections[dict[str,str]|None]] = {}
        self._classname = None  # Current class we are in, if any

        # Initialize the state for this module
        self._attrtyps: dict[str, str] = {}
        self._paramtyps: dict[str, str] = {}
        self._returntyps: dict[str, dict[str, str]] = {}
        state.docstrings[modname] = Sections[dict[str,str]|dict[str,dict[str,str]]](
            params=self._paramtyps,
            returns=self._returntyps,
            attrs=self._attrtyps)
        self._state = state
        assert(state.counters is not None)
        self._counters = state.counters
        self._trace_sig = None

    def _get_obj_name(self, obj) -> str:
        rtn = str(obj)
        if rtn.startswith('<class '):
            # Something like <class 'sklearn.preprocessing._discretization.KBinsDiscretizer'>
            return rtn[rtn.find(' ')+2:-2]
        elif rtn.startswith('<classmethod'):
            # Something like <classmethod(<function DistanceMetric.get_metric at 0x1277800d0>)>
            return rtn[rtn.find('.')+1:].split(' ')[0]
        elif rtn.find(' ') > 0:
            # Something like <function KBinsDiscretizer.__init__ at 0x169e70430>
            return rtn.split(' ')[1]
        else:
            return rtn

    def _update_fullmap(self, section, items, context) -> None:
        if items:
            for name, typ in items.items():
                section[f'{context}.{name}'] = typ

    def _update_full_context(self, sections: Sections[dict[str,str]|None], context: str) -> None:
        """
        As a side effect we collect all of these so they can be 
        written out at the end. This allows us to go from a type
        in the map file to the places it occurs in the source.
        We can also use this in the augmenter to show the tracing
        type annotation whenever we have a mismatch, although
        that is less useful now we are using tracing type annotations
        during this initial phase anyway as the mapped values.
        """
        fullcontext = f'{self._modname}.{context}'   
        self._update_fullmap(_fullmap.params, sections.params, fullcontext)
        self._update_fullmap(_fullmap.attrs, sections.attrs, fullcontext)
        if sections.returns is not None:
            types = list(sections.returns.values())
            if len(sections.returns) == 1:
                _fullmap.returns[context] = types[0]
            elif len(sections.returns) > 1:
                _fullmap.returns[context] = f'tuple[{",".join(types)}]'

    def _analyze_obj(self, obj, context: str) -> Sections[dict[str,str]|None]:
        doc = None
        rtn = Sections[dict[str,str]|None](params=None, returns=None, attrs=None)
        if obj:
            doc = inspect.getdoc(obj)
            if doc:
                rtn = self._parser.parse(doc)
        
        for section, counter in zip(rtn, self._counters):
            if section:
                section = cast(dict[str,str], section)
                counter = cast(Counter[str], counter)
                for typ in section.values():
                    counter[typ] += 1
        return rtn

    @staticmethod
    def get_top_level_obj(mod: ModuleType, fname: str, oname: str) -> Any:
        try:
            return mod.__dict__[oname]
        except KeyError as e:
            try:
                submod = fname[fname.rfind('/')+1:-3]
                return mod.__dict__[submod].__dict__[oname]
            except Exception:
                print(f'{fname}: Could not get obj for {oname}')
                return None

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self._traced_methodsig = None
        rtn = super().visit_ClassDef(node)
        if self.at_top_level_class_level():
            self._classname = node.name.value
            self._state.imports[self._classname] = self._modname
            obj = AnalyzingTransformer.get_top_level_obj(self._mod, self._fname, node.name.value)
            context = self.context()
            docs = self._analyze_obj(obj, context)
            self._docs[context] = docs
            self._update_full_context(docs, context)
        return rtn

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        outer_context = self.context()
        rtn = super().visit_FunctionDef(node)
        name = node.name.value

        if name.startswith('_') and not name.startswith('__'):
            # TODO: make sure in this case we still call leave so that the stack
            # is correct; I am 99% sure we do and this is just to prevent the children
            # being visited.
            return False
        
        obj = None
        context = self.context()
        parent = None
        if self.at_top_level_function_level():
            #context = name
            obj = AnalyzingTransformer.get_top_level_obj(self._mod, self._fname, name)
            self._trace_sig = get_toplevel_function_signature(self._modname, name)
        elif self._classname and self.at_top_level_class_method_level():
            #context = f'{self._classname}.{name}'
            parent = AnalyzingTransformer.get_top_level_obj(self._mod, self._fname, self._classname)
            if parent:
                if name in parent.__dict__:
                    obj = parent.__dict__[name]
                    self._trace_sig = get_method_signature(self._modname, self._classname, name)
                else:
                    print(f'{self._fname}: Could not get obj for {context}')

        docs = self._analyze_obj(obj, context)
        self._docs[context] = docs
        self._update_full_context(docs, context)
        if name == '__init__':
            # If we actually had a docstring with params section, we're done
            if docs and docs.params:
                return rtn
            # Else use the class docstring for __init__
            docs = self._docs.get(outer_context)
            if docs is not None:
                self._docs[context] = docs
                self._update_full_context(docs, context)
            else:
                del self._docs[context]

        return rtn

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.CSTNode:
        # Add a special entry for the return type
        context = self.context()
        doc = self._docs.get(context)
        if doc and doc.returns is not None:
            self._returntyps[context] = doc.returns
            if self._trace_sig is not None and self._trace_sig._return_annotation != inspect._empty:
                if len(doc.returns) == 1:
                    rtype = list(doc.returns.values())[0]
                    if rtype not in self._state.trace_return_types:
                        self._state.trace_return_types[rtype] = set()
                    self._state.trace_return_types[rtype].add(self._trace_sig._return_annotation)
                elif len(doc.returns) > 1:
                    # This should be a tuple; more complex to deal with
                    pass
        self._trace_sig = None
        return super().leave_FunctionDef(original_node, updated_node)

    def visit_Param(self, node: cst.Param) -> bool:
        parent_context = self.context()
        parent_doc = self._docs.get(parent_context)
        rtn = super().visit_Param(node)
        if parent_doc: # and isinstance(parent_doc, DocTypes):
             # The isinstance check makes sure it's not a parameter of a lambda or function that was 
             # assigned as a default value of some other parameter
            param_docs = parent_doc.params
            if param_docs:
                ptype = param_docs.get(node.name.value, None)
                if ptype is not None:
                    self._paramtyps[self.context()] = ptype
                    if self._trace_sig is not None:
                        p = self._trace_sig.parameters.get(node.name.value, None)
                        if p is not None and p.annotation != inspect._empty:
                            if ptype not in self._state.trace_param_types:
                                self._state.trace_param_types[ptype] = set()
                            self._state.trace_param_types[ptype].add(p.annotation)
        return rtn


def _analyze(mod: ModuleType, m: str, fname: str, source: str, state: State, **kwargs) -> State:
    try:
        cstree = cst.parse_module(source)
    except Exception as e:
        raise Exception(f"Failed to parse file: {fname}: {e}")
    try:
        patcher = AnalyzingTransformer(mod, m, fname, state)
        cstree.visit(patcher)
    except Exception as e:
        raise Exception(f"Failed to analyze file: {fname}: {e}")
    return state


from typing import _GenericAlias as GenericAlias, _UnionGenericAlias as UnionType, _type_repr # type: ignore


_qualname = re.compile(r'[A-Za-z_\.]*\.([A-Za-z_][A-Za-z_0-9]*)')


def _adjust_name(name: str) -> str:
    if name in ['List', 'Dict', 'Tuple', 'Set']:
        return name.lower()
    return name


def _get_repr(typ, arraylike: bool = False, matrixlike: bool=False):
    if isinstance(typ, UnionType):
        return '|'.join([_get_repr(a) for a in typ.__args__])
    elif isinstance(typ, GenericAlias) and typ._name and typ.__args__:
        # List, Tuple, etc
        if arraylike and typ._name == 'List':
            return 'ArrayLike'
        return f'{_adjust_name(typ._name)}[{", ".join([_get_repr(a) for a in typ.__args__])}]'
    if arraylike and (typ == np.ndarray or typ == pd.Series):
        return 'ArrayLike'
    if matrixlike and typ in [np.ndarray, pd.DataFrame, scipy.sparse.spmatrix, scipy.sparse.csr_matrix, scipy.sparse.csc_matrix]: # type: ignore 
        return 'MatrixLike'
    if typ == np.int64 or typ == np.uint64:
        return 'Int'
    if typ == np.float32 or typ == np.float64:
        return 'Float'
    typ = _type_repr(typ).replace('NoneType', 'None')
    # Remove module qualifications from classes
    typ = _qualname.sub('\\1', typ)
    return typ


def _combine_types(sigtype: set[type], doctype: str|None) -> str:
    simplified = simplify_types(sigtype)
    arraylike = doctype is not None and doctype.find('ArrayLike') >= 0
    matrixlike = doctype is not None and doctype.find('MatrixLike') >= 0
    # This relies heaviliy on typing module internals
    if not isinstance(simplified, UnionType):
        simplified = _get_repr(simplified, arraylike, matrixlike)
        return simplified if doctype is None or doctype == simplified else f'{simplified}|{doctype}'
    
    components = [_get_repr(a, arraylike, matrixlike) for a in simplified.__args__] # type: ignore
    # Remove some redundant types
    if 'Float' in components:
        components = [c for c in components if c not in ['Int', 'int', 'float', 'None']]
    elif 'Int' in components:
        components = [c for c in components if c not in ['int', 'None']]
    else:
        components = [c for c in components if c != 'None']
    if doctype is not None:
        components.append(doctype)
    return '|'.join(set(components))


def _post_process(m: str, state: State, include_counts: bool = True, dump_all = True) -> Sections[str]:
    print("Analyzing and normalizing types...")
    maps = load_type_maps(m)
    results = [[], [], []]
    assert(state.counters is not None)
    freqs: Sections[Counter[str]] = state.counters
    imports: dict = state.imports
    total_trivial = 0
    total_mapped = 0
    total_missed = 0
    trivials = {}
    for section, result, freq, map in zip(['params', 'returns', 'attrs'], results, freqs, maps):
        freq = cast(Counter[str], freq)
        map = cast(dict[str, str], map)
        for typ, cnt in freq.most_common():
            if typ in map:
                total_mapped += cnt
            else:
                normtype, _ = normalize_type(typ, m, imports, section=='params')
                sigtype = None
                if section == 'params' and typ in state.trace_param_types:
                    sigtype = state.trace_param_types[typ]
                elif section == 'returns' and typ in state.trace_return_types:
                    sigtype = state.trace_return_types[typ]
                if normtype is None:
                    normtype = typ if sigtype is None else _combine_types(sigtype, None)
                elif sigtype is not None:
                    normtype = _combine_types(sigtype, normtype)
                trivial = is_trivial(typ, m, imports)
                if not dump_all and trivial:
                    trivials[typ] = normtype
                    total_trivial += cnt
                else:
                    total_missed += cnt
                    if include_counts:
                        result.append(f'{"@" if trivial else ""}{cnt}#{typ}#{normtype}\n')
                    else:
                        result.append(f'{"@" if trivial else ""}#{typ}#{normtype}\n')
    print(f'Trivial: {total_trivial}, Mapped: {total_mapped}, Missed: {total_missed}')
    print('\nTRIVIALS\n')
    for k, v in trivials.items():
        print(f'{k}#{v}')

    print_norm1()

    save_fullmap('analysis', m, _fullmap)

    return Sections[str](params=''.join(results[0]), 
                    returns=''.join(results[1]),
                    attrs=''.join(results[2]))


def _targeter(m: str, suffix: str) -> str:
    """ Turn module name into map file name """
    return f"analysis/{m}.{suffix}.map.missing"


def analyze_module(m: str, include_submodules: bool = True, include_counts = True, dump_all = True, trace_folder='tracing') -> None|State:
    print("Gathering docstrings")
    init_trace_loader(trace_folder, m)
    state = State(
        Sections[Counter[str]](params=Counter(), returns=Counter(), attrs=Counter()),
        {}, 
        {}, 
        load_type_maps(m), 
        {}, 
        {})
    
    if process_module(m, state, 
            _analyze, _targeter, 
            post_processor=_post_process,
            include_submodules=include_submodules,
            include_counts=include_counts,
            dump_all=dump_all) is not None:
        save_import_map(m, state.imports)
        save_docstrings(m, state.docstrings)
        return state
    
    return None
