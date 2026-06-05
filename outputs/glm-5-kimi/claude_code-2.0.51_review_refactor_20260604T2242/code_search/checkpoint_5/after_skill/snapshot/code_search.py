#!/usr/bin/env python3
"""
Command-line code searcher for Python codebases.
Searches for matches based on rules (exact match, regex, pattern, or selector) and outputs JSON Lines.
Supports auto-fixes with --dry-run and --apply-fixes options.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_cpp
import tree_sitter_rust
import tree_sitter_java
import tree_sitter_go
import tree_sitter_haskell
from tree_sitter import Language, Parser


LANGUAGE_MAP = {
    'python': Language(tree_sitter_python.language()),
    'javascript': Language(tree_sitter_javascript.language()),
    'cpp': Language(tree_sitter_cpp.language()),
    'rust': Language(tree_sitter_rust.language()),
    'java': Language(tree_sitter_java.language()),
    'go': Language(tree_sitter_go.language()),
    'haskell': Language(tree_sitter_haskell.language()),
}

FILE_EXTENSION_MAP = {
    '.py': 'python',
    '.js': 'javascript',
    '.mjs': 'javascript',
    '.cjs': 'javascript',
    '.cpp': 'cpp',
    '.cxx': 'cpp',
    '.cc': 'cpp',
    '.hh': 'cpp',
    '.hpp': 'cpp',
    '.hxx': 'cpp',
    '.rs': 'rust',
    '.java': 'java',
    '.go': 'go',
    '.hs': 'haskell',
    '.lhs': 'haskell',
}

PARSER_CACHE: Dict[str, Parser] = {}

# Valid node types for selector rules
VALID_SELECTORS = {
    'program', 'module', 'namespace', 'import', 'export', 'declaration', 'definition',
    'variable_declaration', 'constant_declaration', 'type_declaration', 'function_declaration',
    'method_declaration', 'class_declaration', 'interface_declaration', 'struct_declaration',
    'enum_declaration', 'field_declaration', 'type', 'generic_type', 'parameter', 'argument',
    'block', 'statement', 'return_statement', 'throw_statement', 'break_statement',
    'continue_statement', 'if_statement', 'else_clause', 'switch_statement', 'case_clause',
    'for_statement', 'while_statement', 'do_while_statement', 'try_statement', 'catch_clause',
    'finally_clause', 'expression', 'assignment_expression', 'call_expression', 'member_expression',
    'subscript_expression', 'binary_expression', 'unary_expression', 'conditional_expression',
    'literal', 'string_literal', 'numeric_literal', 'boolean_literal', 'null_literal',
    'array_literal', 'object_literal', 'annotation', 'decorator', 'attribute', 'access_modifier',
    'operator', 'identifier', 'comment'
}

# Mapping from generic selector types to language-specific node types
SELECTOR_NODE_TYPE_MAP = {
    'python': {
        'program': ('module',),
        'module': ('module',),
        'namespace': (),  # Python doesn't have namespaces as AST nodes
        'import': ('import_statement', 'import_from_statement'),
        'export': (),  # Python doesn't have exports
        'declaration': ('function_definition', 'class_definition', 'import_statement', 'import_from_statement'),
        'definition': ('function_definition', 'class_definition'),
        'variable_declaration': ('expression_statement', 'assignment', 'augmented_assignment', 'named_expression'),
        'constant_declaration': (),  # Python doesn't distinguish constants
        'type_declaration': ('class_definition',),
        'function_declaration': ('function_definition',),
        'method_declaration': ('function_definition',),  # Methods are just functions in classes
        'class_declaration': ('class_definition',),
        'interface_declaration': (),  # Python doesn't have interfaces
        'struct_declaration': (),  # Python doesn't have structs
        'enum_declaration': (),  # Python doesn't have enums as AST nodes
        'field_declaration': ('expression_statement',),  # Class attributes
        'type': ('type',),
        'generic_type': ('subscript',),
        'parameter': ('parameters', 'default_parameter', 'typed_parameter', 'typed_default_parameter', 'dictionary_splat_pattern', 'list_splat_pattern'),
        'argument': ('argument_list', 'call'),
        'block': ('block', 'function_definition', 'class_definition', 'if_statement', 'for_statement', 'while_statement', 'try_statement', 'with_statement'),
        'statement': ('expression_statement', 'return_statement', 'raise_statement', 'break_statement', 'continue_statement', 'if_statement', 'for_statement', 'while_statement', 'try_statement', 'with_statement', 'import_statement', 'import_from_statement', 'assert_statement', 'delete_statement', 'pass_statement', 'global_statement', 'nonlocal_statement'),
        'return_statement': ('return_statement',),
        'throw_statement': ('raise_statement',),
        'break_statement': ('break_statement',),
        'continue_statement': ('continue_statement',),
        'if_statement': ('if_statement',),
        'else_clause': ('else_clause', 'elif_clause'),
        'switch_statement': (),  # Python doesn't have switch
        'case_clause': (),  # Python doesn't have case (except match/case in 3.10+)
        'for_statement': ('for_statement',),
        'while_statement': ('while_statement',),
        'do_while_statement': (),  # Python doesn't have do-while
        'try_statement': ('try_statement',),
        'catch_clause': ('except_clause',),
        'finally_clause': ('finally_clause',),
        'expression': ('expression_statement', 'call', 'binary_operator', 'unary_operator', 'comparison', 'boolean_operator', 'assignment', 'augmented_assignment', 'named_expression', 'attribute', 'subscript', 'identifier', 'string', 'integer', 'float', 'true', 'false', 'none', 'list', 'dictionary', 'set', 'tuple', 'lambda', 'conditional_expression', 'await', 'yield'),
        'assignment_expression': ('assignment', 'augmented_assignment', 'named_expression'),
        'call_expression': ('call',),
        'member_expression': ('attribute',),
        'subscript_expression': ('subscript',),
        'binary_expression': ('binary_operator', 'comparison', 'boolean_operator'),
        'unary_expression': ('unary_operator', 'not_operator'),
        'conditional_expression': ('conditional_expression',),
        'literal': ('string', 'integer', 'float', 'true', 'false', 'none', 'list', 'dictionary', 'set', 'tuple'),
        'string_literal': ('string',),
        'numeric_literal': ('integer', 'float'),
        'boolean_literal': ('true', 'false'),
        'null_literal': ('none',),
        'array_literal': ('list',),
        'object_literal': ('dictionary',),
        'annotation': ('type',),
        'decorator': ('decorator',),
        'attribute': ('attribute',),
        'access_modifier': (),  # Python doesn't have access modifiers
        'operator': ('binary_operator', 'unary_operator', 'comparison', 'boolean_operator'),
        'identifier': ('identifier',),
        'comment': ('comment',),
    },
    'javascript': {
        'program': ('program',),
        'module': ('program',),
        'namespace': (),  # JS doesn't have namespaces
        'import': ('import_statement',),
        'export': ('export_statement',),
        'declaration': ('function_declaration', 'class_declaration', 'variable_declaration', 'import_statement', 'export_statement'),
        'definition': ('function_declaration', 'class_declaration', 'variable_declaration'),
        'variable_declaration': ('variable_declaration', 'lexical_declaration'),
        'constant_declaration': ('lexical_declaration',),
        'type_declaration': (),  # TypeScript would have this, but we're handling JS
        'function_declaration': ('function_declaration', 'function_expression', 'arrow_function', 'generator_function', 'generator_function_declaration'),
        'method_declaration': ('method_definition',),
        'class_declaration': ('class_declaration', 'class'),
        'interface_declaration': (),  # TypeScript
        'struct_declaration': (),  # Not in JS
        'enum_declaration': (),  # TypeScript
        'field_declaration': ('public_field_definition', 'field_definition'),
        'type': ('type_identifier', 'predefined_type', 'generic_type'),
        'generic_type': ('generic_type', 'type_arguments'),
        'parameter': ('formal_parameters', 'required_parameter', 'optional_parameter', 'rest_parameter'),
        'argument': ('arguments',),
        'block': ('statement_block', 'block'),
        'statement': ('expression_statement', 'return_statement', 'throw_statement', 'break_statement', 'continue_statement', 'if_statement', 'switch_statement', 'for_statement', 'for_in_statement', 'while_statement', 'do_statement', 'try_statement', 'with_statement', 'import_statement', 'export_statement', 'variable_declaration', 'lexical_declaration', 'debugger_statement', 'empty_statement', 'labeled_statement'),
        'return_statement': ('return_statement',),
        'throw_statement': ('throw_statement',),
        'break_statement': ('break_statement',),
        'continue_statement': ('continue_statement',),
        'if_statement': ('if_statement',),
        'else_clause': ('else_clause',),
        'switch_statement': ('switch_statement',),
        'case_clause': ('switch_case', 'switch_default'),
        'for_statement': ('for_statement', 'for_in_statement', 'for_of_statement'),
        'while_statement': ('while_statement',),
        'do_while_statement': ('do_statement',),
        'try_statement': ('try_statement',),
        'catch_clause': ('catch_clause',),
        'finally_clause': ('finally_clause',),
        'expression': ('expression_statement', 'call_expression', 'member_expression', 'subscript_expression', 'binary_expression', 'unary_expression', 'assignment_expression', 'augmented_assignment_expression', 'ternary_expression', 'arrow_function', 'identifier', 'string', 'number', 'true', 'false', 'null', 'undefined', 'array', 'object', 'new_expression', 'await_expression', 'yield_expression', 'parenthesized_expression'),
        'assignment_expression': ('assignment_expression', 'augmented_assignment_expression'),
        'call_expression': ('call_expression', 'new_expression'),
        'member_expression': ('member_expression',),
        'subscript_expression': ('subscript_expression',),
        'binary_expression': ('binary_expression',),
        'unary_expression': ('unary_expression',),
        'conditional_expression': ('ternary_expression',),
        'literal': ('string', 'number', 'template_string', 'true', 'false', 'null', 'undefined', 'array', 'object', 'regex'),
        'string_literal': ('string', 'template_string'),
        'numeric_literal': ('number',),
        'boolean_literal': ('true', 'false'),
        'null_literal': ('null', 'undefined'),
        'array_literal': ('array',),
        'object_literal': ('object', 'object_pattern'),
        'annotation': (),  # TypeScript
        'decorator': ('decorator',),
        'attribute': (),  # JS doesn't have attributes
        'access_modifier': (),  # TypeScript
        'operator': ('binary_expression', 'unary_expression'),
        'identifier': ('identifier', 'property_identifier'),
        'comment': ('comment',),
    },
    'cpp': {
        'program': ('translation_unit',),
        'module': ('translation_unit',),
        'namespace': ('namespace_definition', 'using_declaration', 'namespace_alias_definition'),
        'import': ('preproc_include', 'using_declaration'),
        'export': (),  # C++20 modules (may not be fully supported)
        'declaration': ('declaration', 'function_definition', 'class_specifier', 'struct_specifier', 'enum_specifier', 'namespace_definition', 'preproc_include'),
        'definition': ('function_definition', 'class_specifier', 'struct_specifier', 'enum_specifier'),
        'variable_declaration': ('declaration', 'init_declarator', 'field_declaration'),
        'constant_declaration': ('declaration', 'init_declarator'),
        'type_declaration': ('class_specifier', 'struct_specifier', 'enum_specifier', 'type_definition', 'alias_declaration'),
        'function_declaration': ('declaration', 'function_definition', 'function_declarator'),
        'method_declaration': ('function_definition', 'declaration', 'function_declarator'),
        'class_declaration': ('class_specifier',),
        'interface_declaration': (),  # C++ doesn't have interfaces per se
        'struct_declaration': ('struct_specifier',),
        'enum_declaration': ('enum_specifier',),
        'field_declaration': ('field_declaration', 'field_declarator'),
        'type': ('primitive_type', 'type_identifier', 'sized_type_specifier', 'type_descriptor'),
        'generic_type': ('template_type', 'template_argument_list'),
        'parameter': ('parameter_declaration', 'optional_parameter_declaration', 'variadic_parameter_declaration'),
        'argument': ('argument_list', 'call_expression'),
        'block': ('compound_statement', 'declaration_list', 'field_declaration_list'),
        'statement': ('expression_statement', 'return_statement', 'throw_statement', 'break_statement', 'continue_statement', 'if_statement', 'switch_statement', 'case_statement', 'for_statement', 'for_range_statement', 'while_statement', 'do_statement', 'try_statement', 'declaration'),
        'return_statement': ('return_statement',),
        'throw_statement': ('throw_statement',),
        'break_statement': ('break_statement',),
        'continue_statement': ('continue_statement',),
        'if_statement': ('if_statement',),
        'else_clause': ('else_clause',),
        'switch_statement': ('switch_statement',),
        'case_clause': ('case_statement',),
        'for_statement': ('for_statement', 'for_range_statement'),
        'while_statement': ('while_statement',),
        'do_while_statement': ('do_statement',),
        'try_statement': ('try_statement',),
        'catch_clause': ('catch_clause',),
        'finally_clause': (),  # C++ doesn't have finally
        'expression': ('expression_statement', 'call_expression', 'field_expression', 'subscript_expression', 'binary_expression', 'unary_expression', 'assignment_expression', 'conditional_expression', 'identifier', 'string_literal', 'number_literal', 'true', 'false', 'null', 'pointer_expression', 'reference_expression', 'new_expression', 'delete_expression', 'cast_expression', 'sizeof_expression', 'lambda_expression', 'parenthesized_expression'),
        'assignment_expression': ('assignment_expression',),
        'call_expression': ('call_expression',),
        'member_expression': ('field_expression',),
        'subscript_expression': ('subscript_expression',),
        'binary_expression': ('binary_expression',),
        'unary_expression': ('unary_expression', 'pointer_expression', 'reference_expression'),
        'conditional_expression': ('conditional_expression',),
        'literal': ('string_literal', 'number_literal', 'true', 'false', 'null', 'nullptr', 'initializer_list', 'raw_string_literal', 'char_literal'),
        'string_literal': ('string_literal', 'raw_string_literal', 'char_literal'),
        'numeric_literal': ('number_literal',),
        'boolean_literal': ('true', 'false'),
        'null_literal': ('null', 'nullptr'),
        'array_literal': ('initializer_list',),
        'object_literal': ('initializer_list',),
        'annotation': (),  # C++ doesn't have annotations
        'decorator': (),  # C++ doesn't have decorators
        'attribute': ('attribute', 'attribute_specifier'),
        'access_modifier': ('access_specifier',),
        'operator': ('binary_expression', 'unary_expression'),
        'identifier': ('identifier', 'type_identifier', 'field_identifier', 'namespace_identifier'),
        'comment': ('comment',),
    },
    'rust': {
        'program': ('source_file',),
        'module': ('source_file', 'mod_item'),
        'namespace': ('mod_item',),
        'import': ('use_declaration',),
        'export': (),
        'declaration': ('function_item', 'struct_item', 'enum_item', 'trait_item', 'impl_item', 'const_item', 'static_item', 'type_item', 'mod_item', 'use_declaration'),
        'definition': ('function_item', 'struct_item', 'enum_item', 'trait_item', 'impl_item'),
        'variable_declaration': ('let_declaration', 'let_expression'),
        'constant_declaration': ('const_item', 'static_item'),
        'type_declaration': ('struct_item', 'enum_item', 'type_item', 'trait_item'),
        'function_declaration': ('function_item', 'function_signature_item'),
        'method_declaration': ('function_item',),
        'class_declaration': ('struct_item',),
        'interface_declaration': ('trait_item',),
        'struct_declaration': ('struct_item',),
        'enum_declaration': ('enum_item',),
        'field_declaration': ('field_declaration', 'tuple_field_declaration', 'enum_variant'),
        'type': ('type_identifier', 'primitive_type', 'generic_type', 'bounded_type', 'array_type', 'reference_type', 'pointer_type', 'tuple_type'),
        'generic_type': ('generic_type', 'type_arguments', 'bounded_type'),
        'parameter': ('parameter', 'self_parameter', 'variadic_parameter'),
        'argument': ('arguments', 'call_expression'),
        'block': ('block',),
        'statement': ('expression_statement', 'let_declaration', 'return_expression', 'break_expression', 'continue_expression', 'if_expression', 'match_expression', 'for_expression', 'while_expression', 'loop_expression'),
        'return_statement': ('return_expression',),
        'throw_statement': (),
        'break_statement': ('break_expression',),
        'continue_statement': ('continue_expression',),
        'if_statement': ('if_expression', 'else_clause'),
        'else_clause': ('else_clause',),
        'switch_statement': ('match_expression',),
        'case_clause': ('match_arm',),
        'for_statement': ('for_expression',),
        'while_statement': ('while_expression',),
        'do_while_statement': ('loop_expression',),
        'try_statement': (),
        'catch_clause': (),
        'finally_clause': (),
        'expression': ('call_expression', 'field_expression', 'index_expression', 'binary_expression', 'unary_expression', 'assignment_expression', 'compound_assignment_expr', 'try_expression', 'await_expression', 'range_expression', 'reference_expression', 'dereference_expression', 'cast_expression', 'type_cast_expression', 'identifier', 'string_literal', 'raw_string_literal', 'integer_literal', 'float_literal', 'boolean_literal', 'tuple_expression', 'array_expression', 'struct_expression', 'closure_expression', 'parenthesized_expression', 'macro_invocation'),
        'assignment_expression': ('assignment_expression', 'compound_assignment_expr'),
        'call_expression': ('call_expression', 'macro_invocation', 'macro_invocation2'),
        'member_expression': ('field_expression', 'scoped_identifier'),
        'subscript_expression': ('index_expression',),
        'binary_expression': ('binary_expression',),
        'unary_expression': ('unary_expression', 'reference_expression', 'dereference_expression', 'try_expression'),
        'conditional_expression': ('if_expression',),
        'literal': ('string_literal', 'raw_string_literal', 'integer_literal', 'float_literal', 'boolean_literal', 'tuple_expression', 'array_expression', 'struct_expression', 'char_literal'),
        'string_literal': ('string_literal', 'raw_string_literal', 'char_literal'),
        'numeric_literal': ('integer_literal', 'float_literal'),
        'boolean_literal': ('boolean_literal',),
        'null_literal': (),
        'array_literal': ('array_expression',),
        'object_literal': ('struct_expression',),
        'annotation': ('attribute_item', 'inner_attribute_item', 'outer_attribute_item'),
        'decorator': ('attribute_item', 'inner_attribute_item', 'outer_attribute_item'),
        'attribute': ('attribute_item', 'inner_attribute_item', 'outer_attribute_item', 'meta_item'),
        'access_modifier': ('visibility_modifier',),
        'operator': ('binary_expression', 'unary_expression'),
        'identifier': ('identifier', 'type_identifier', 'field_identifier'),
        'comment': ('line_comment', 'block_comment'),
    },
    'java': {
        'program': ('compilation_unit',),
        'module': ('compilation_unit', 'module_declaration'),
        'namespace': ('package_declaration',),
        'import': ('import_declaration',),
        'export': (),
        'declaration': ('method_declaration', 'class_declaration', 'interface_declaration', 'enum_declaration', 'annotation_type_declaration', 'field_declaration', 'constructor_declaration', 'import_declaration', 'package_declaration'),
        'definition': ('method_declaration', 'class_declaration', 'interface_declaration', 'enum_declaration', 'annotation_type_declaration', 'constructor_declaration'),
        'variable_declaration': ('local_variable_declaration', 'field_declaration', 'variable_declarator'),
        'constant_declaration': ('field_declaration',),
        'type_declaration': ('class_declaration', 'interface_declaration', 'enum_declaration', 'annotation_type_declaration'),
        'function_declaration': ('method_declaration', 'constructor_declaration'),
        'method_declaration': ('method_declaration',),
        'class_declaration': ('class_declaration',),
        'interface_declaration': ('interface_declaration',),
        'struct_declaration': (),
        'enum_declaration': ('enum_declaration',),
        'field_declaration': ('field_declaration',),
        'type': ('type_identifier', 'integral_type', 'floating_point_type', 'boolean_type', 'void_type', 'generic_type', 'array_type', 'type_argument'),
        'generic_type': ('generic_type', 'type_argument', 'type_parameter'),
        'parameter': ('formal_parameter', 'variadic_parameter'),
        'argument': ('argument_list', 'call_expression'),
        'block': ('block', 'class_body', 'interface_body', 'enum_body'),
        'statement': ('expression_statement', 'return_statement', 'throw_statement', 'break_statement', 'continue_statement', 'if_statement', 'switch_statement', 'for_statement', 'enhanced_for_statement', 'while_statement', 'do_statement', 'try_statement', 'try_with_resources_statement', 'synchronized_statement', 'local_variable_declaration'),
        'return_statement': ('return_statement',),
        'throw_statement': ('throw_statement',),
        'break_statement': ('break_statement',),
        'continue_statement': ('continue_statement',),
        'if_statement': ('if_statement',),
        'else_clause': ('else_clause',),
        'switch_statement': ('switch_statement',),
        'case_clause': ('switch_block_statement_group', 'switch_label', 'switch_case'),
        'for_statement': ('for_statement', 'enhanced_for_statement'),
        'while_statement': ('while_statement',),
        'do_while_statement': ('do_statement',),
        'try_statement': ('try_statement', 'try_with_resources_statement'),
        'catch_clause': ('catch_clause', 'catch_formal_parameter'),
        'finally_clause': ('finally_clause',),
        'expression': ('expression_statement', 'method_invocation', 'field_access', 'array_access', 'binary_expression', 'unary_expression', 'assignment_expression', 'conditional_expression', 'instanceof_expression', 'cast_expression', 'identifier', 'string_literal', 'decimal_integer_literal', 'hex_integer_literal', 'octal_integer_literal', 'binary_integer_literal', 'decimal_floating_point_literal', 'hex_floating_point_literal', 'boolean_literal', 'null_literal', 'array_creation_expression', 'class_literal', 'parenthesized_expression', 'lambda_expression', 'method_reference'),
        'assignment_expression': ('assignment_expression',),
        'call_expression': ('method_invocation',),
        'member_expression': ('field_access', 'scoped_identifier'),
        'subscript_expression': ('array_access',),
        'binary_expression': ('binary_expression',),
        'unary_expression': ('unary_expression',),
        'conditional_expression': ('conditional_expression', 'ternary_expression'),
        'literal': ('string_literal', 'decimal_integer_literal', 'hex_integer_literal', 'octal_integer_literal', 'binary_integer_literal', 'decimal_floating_point_literal', 'hex_floating_point_literal', 'boolean_literal', 'null_literal', 'character_literal'),
        'string_literal': ('string_literal',),
        'numeric_literal': ('decimal_integer_literal', 'hex_integer_literal', 'octal_integer_literal', 'binary_integer_literal', 'decimal_floating_point_literal', 'hex_floating_point_literal'),
        'boolean_literal': ('boolean_literal',),
        'null_literal': ('null_literal',),
        'array_literal': ('array_initializer',),
        'object_literal': (),
        'annotation': ('annotation', 'marker_annotation', 'normal_annotation', 'single_element_annotation'),
        'decorator': ('annotation', 'marker_annotation', 'normal_annotation', 'single_element_annotation'),
        'attribute': ('annotation', 'marker_annotation', 'normal_annotation', 'single_element_annotation'),
        'access_modifier': ('modifier',),
        'operator': ('binary_expression', 'unary_expression'),
        'identifier': ('identifier', 'type_identifier', 'field_identifier'),
        'comment': ('line_comment', 'block_comment'),
    },
    'go': {
        'program': ('source_file',),
        'module': ('source_file', 'package_clause'),
        'namespace': (),
        'import': ('import_declaration', 'import_spec'),
        'export': (),
        'declaration': ('function_declaration', 'method_declaration', 'type_declaration', 'type_spec', 'var_declaration', 'const_declaration', 'import_declaration'),
        'definition': ('function_declaration', 'method_declaration', 'type_declaration', 'type_spec'),
        'variable_declaration': ('var_declaration', 'var_spec', 'short_var_declaration'),
        'constant_declaration': ('const_declaration', 'const_spec'),
        'type_declaration': ('type_declaration', 'type_spec'),
        'function_declaration': ('function_declaration', 'method_declaration', 'func_literal'),
        'method_declaration': ('method_declaration', 'method_spec', 'method_elem'),
        'class_declaration': (),
        'interface_declaration': ('type_declaration', 'interface_type'),
        'struct_declaration': ('type_declaration', 'struct_type'),
        'enum_declaration': ('const_declaration', 'const_spec'),  # Go uses iota for enums
        'field_declaration': ('field_declaration', 'field_declaration_list', 'parameter_declaration', 'var_spec'),
        'type': ('type_identifier', 'qualified_type', 'pointer_type', 'slice_type', 'array_type', 'struct_type', 'interface_type', 'map_type', 'channel_type', 'function_type'),
        'generic_type': ('type_arguments', 'type_parameter_list', 'type_constraint'),
        'parameter': ('parameter_declaration', 'parameter_list', 'variadic_parameter_declaration'),
        'argument': ('argument_list', 'call_expression'),
        'block': ('block', 'source_file'),
        'statement': ('expression_statement', 'return_statement', 'break_statement', 'continue_statement', 'goto_statement', 'if_statement', 'switch_statement', 'type_switch_statement', 'select_statement', 'for_statement', 'for_clause', 'range_clause', 'var_declaration', 'short_var_declaration', 'defer_statement', 'go_statement', 'send_statement', 'labeled_statement'),
        'return_statement': ('return_statement',),
        'throw_statement': (),
        'break_statement': ('break_statement',),
        'continue_statement': ('continue_statement',),
        'if_statement': ('if_statement',),
        'else_clause': ('else_clause',),
        'switch_statement': ('switch_statement', 'type_switch_statement'),
        'case_clause': ('case_clause', 'type_case_clause', 'communication_case'),
        'for_statement': ('for_statement', 'for_clause', 'range_clause'),
        'while_statement': ('for_statement',),
        'do_while_statement': (),
        'try_statement': (),
        'catch_clause': (),
        'finally_clause': (),
        'expression': ('expression_statement', 'call_expression', 'selector_expression', 'index_expression', 'slice_expression', 'binary_expression', 'unary_expression', 'assignment_expression', 'composite_literal', 'identifier', 'interpreted_string_literal', 'raw_string_literal', 'int_literal', 'float_literal', 'imaginary_literal', 'rune_literal', 'true', 'false', 'nil', 'array_literal', 'struct_literal', 'map_literal', 'slice_literal', 'function_literal', 'type_assertion_expression', 'type_conversion_expression', 'parenthesized_expression'),
        'assignment_expression': ('assignment_expression',),
        'call_expression': ('call_expression',),
        'member_expression': ('selector_expression', 'field_identifier'),
        'subscript_expression': ('index_expression', 'slice_expression'),
        'binary_expression': ('binary_expression',),
        'unary_expression': ('unary_expression',),
        'conditional_expression': (),
        'literal': ('interpreted_string_literal', 'raw_string_literal', 'int_literal', 'float_literal', 'imaginary_literal', 'rune_literal', 'true', 'false', 'nil', 'array_literal', 'struct_literal', 'map_literal', 'slice_literal', 'composite_literal'),
        'string_literal': ('interpreted_string_literal', 'raw_string_literal'),
        'numeric_literal': ('int_literal', 'float_literal', 'imaginary_literal'),
        'boolean_literal': ('true', 'false'),
        'null_literal': ('nil',),
        'array_literal': ('array_literal', 'slice_literal', 'composite_literal'),
        'object_literal': ('struct_literal', 'map_literal', 'composite_literal'),
        'annotation': (),
        'decorator': (),
        'attribute': (),
        'access_modifier': (),
        'operator': ('binary_expression', 'unary_expression'),
        'identifier': ('identifier', 'type_identifier', 'field_identifier', 'package_identifier'),
        'comment': ('comment',),
    },
    'haskell': {
        'program': ('module',),
        'module': ('module',),
        'namespace': (),
        'import': ('import',),
        'export': ('export', 'export_list'),
        'declaration': ('function', 'signature', 'type_alias', 'data_type', 'newtype', 'class_declaration', 'instance_declaration', 'import'),
        'definition': ('function', 'signature', 'type_alias', 'data_type', 'newtype'),
        'variable_declaration': ('function', 'pat_binding'),
        'constant_declaration': ('function', 'pat_binding'),
        'type_declaration': ('type_alias', 'data_type', 'newtype', 'class_declaration'),
        'function_declaration': ('function', 'signature'),
        'method_declaration': ('function',),
        'class_declaration': ('class_declaration', 'class_instance'),
        'interface_declaration': ('class_declaration',),
        'struct_declaration': ('data_type', 'newtype'),
        'enum_declaration': ('data_type',),
        'field_declaration': ('field_declaration', 'constructor'),
        'type': ('type', 'type_variable', 'type_con', 'tycon_arrow', 'tuple_type', 'list_type'),
        'generic_type': ('type', 'type_variable'),
        'parameter': ('patterns', 'pattern', 'function_param', 'function_params'),
        'argument': ('arguments', 'apply',),
        'block': ('block', 'do_block', 'case_block', 'let_block'),
        'statement': ('function', 'pat_binding', 'signature', 'exp', 'do_statement', 'let_statement'),
        'return_statement': (),
        'throw_statement': (),
        'break_statement': (),
        'continue_statement': (),
        'if_statement': ('if',),
        'else_clause': (),
        'switch_statement': ('case', 'case_block'),
        'case_clause': ('alt', 'guarded_alt', 'alternative'),
        'for_statement': (),
        'while_statement': (),
        'do_while_statement': (),
        'try_statement': (),
        'catch_clause': (),
        'finally_clause': (),
        'expression': ('exp', 'apply', 'infix_apply', 'negation', 'exp_negation', 'exp_negation_infix', 'prefix_id', 'operator', 'exp_infix', 'exp_record', 'exp_tuple', 'exp_list', 'exp_cond', 'lambda', 'exp_lambda', 'exp_let', 'exp_if', 'exp_case', 'exp_do', 'exp_type_sig', 'exp_th_quasiquote', 'exp_th_splice', 'exp_th_bracket', 'var', 'variable', 'con', 'constructor', 'integer', 'string', 'char', 'float', 'literal', 'unit', 'parenthesized_exp'),
        'assignment_expression': ('pat_binding',),
        'call_expression': ('apply', 'infix_apply'),
        'member_expression': ('exp_record', 'field_name'),
        'subscript_expression': ('exp_list',),
        'binary_expression': ('infix_apply',),
        'unary_expression': ('negation',),
        'conditional_expression': ('if', 'exp_cond', 'exp_if'),
        'literal': ('integer', 'string', 'char', 'float', 'literal', 'unit'),
        'string_literal': ('string',),
        'numeric_literal': ('integer', 'float'),
        'boolean_literal': (),
        'null_literal': (),
        'array_literal': ('exp_list', 'list'),
        'object_literal': ('exp_record', 'record'),
        'annotation': ('exp_type_sig', 'type_sig',),
        'decorator': (),
        'attribute': (),
        'access_modifier': (),
        'operator': ('operator', 'infix_apply'),
        'identifier': ('var', 'variable', 'con', 'constructor', 'function_name'),
        'comment': ('comment', 'block_comment', 'haddock_comment'),
    }
}


def get_parser(language: str) -> Parser:
    """Get or create a parser for the given language."""
    if language not in PARSER_CACHE:
        parser = Parser(LANGUAGE_MAP[language])
        PARSER_CACHE[language] = parser
    return PARSER_CACHE[language]


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Search codebase for pattern matches"
    )
    parser.add_argument(
        "root_dir",
        help="Path to the codebase to scan"
    )
    parser.add_argument(
        "--rules",
        required=True,
        help="Path to JSON file containing rules array"
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="File encoding (default: utf-8)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview fixes without writing to disk"
    )
    parser.add_argument(
        "--apply-fixes",
        action="store_true",
        help="Apply fixes and write changes to disk"
    )

    args = parser.parse_args()

    # Validate mutual exclusivity
    if args.dry_run and args.apply_fixes:
        parser.error("--dry-run and --apply-fixes are mutually exclusive")

    return args


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    """Load and validate rules from JSON file."""
    with open(rules_path, 'r', encoding='utf-8') as f:
        rules = json.load(f)

    if not isinstance(rules, list):
        raise ValueError("Rules file must contain a JSON array")

    # Validate and normalize rules
    seen_ids: Set[str] = set()
    validated_rules = []
    valid_languages = {'python', 'javascript', 'cpp', 'rust', 'java', 'go', 'haskell'}
    valid_kinds = {'exact', 'regex', 'pattern', 'selector'}

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule at index {i} must be an object")

        # Validate id
        if 'id' not in rule or not isinstance(rule['id'], str) or not rule['id']:
            raise ValueError(f"Rule at index {i} must have a non-empty string 'id'")
        if rule['id'] in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule['id']}")
        seen_ids.add(rule['id'])

        # Validate kind
        if 'kind' not in rule or rule['kind'] not in valid_kinds:
            raise ValueError(f"Rule at index {i} must have kind 'exact', 'regex', 'pattern', or 'selector'")

        # Validate pattern for non-selector rules
        if rule['kind'] in {'exact', 'regex', 'pattern'}:
            if 'pattern' not in rule or not isinstance(rule['pattern'], str) or not rule['pattern']:
                raise ValueError(f"Rule at index {i} with kind '{rule['kind']}' must have a non-empty string 'pattern'")

        # Validate selector for selector rules
        if rule['kind'] == 'selector':
            if 'selector' not in rule or not isinstance(rule['selector'], str) or not rule['selector']:
                raise ValueError(f"Rule at index {i} with kind 'selector' must have a non-empty string 'selector'")
            if rule['selector'] not in VALID_SELECTORS:
                raise ValueError(f"Rule at index {i} has invalid selector '{rule['selector']}'")

        # Validate languages (optional)
        languages = rule.get('languages', ['python', 'javascript', 'cpp', 'rust', 'java', 'go', 'haskell'])
        if not isinstance(languages, list):
            raise ValueError(f"Rule {rule['id']}: 'languages' must be an array")
        if not languages:
            languages = ['python', 'javascript', 'cpp', 'rust', 'java', 'go', 'haskell']
        for lang in languages:
            if not isinstance(lang, str):
                raise ValueError(f"Rule {rule['id']}: 'languages' must contain strings")
            if lang not in valid_languages:
                raise ValueError(f"Rule {rule['id']}: 'languages' may only contain 'python', 'javascript', 'cpp', 'rust', 'java', 'go', 'haskell'")

        # Validate regex_flags (optional, only for regex)
        regex_flags = rule.get('regex_flags', [])
        if rule['kind'] == 'regex':
            if not isinstance(regex_flags, list):
                raise ValueError(f"Rule {rule['id']}: 'regex_flags' must be an array")
            valid_flags = {'i', 'm', 's'}
            for flag in regex_flags:
                if flag not in valid_flags:
                    raise ValueError(f"Rule {rule['id']}: 'regex_flags' may only contain 'i', 'm', 's'")
        elif regex_flags:
            raise ValueError(f"Rule {rule['id']}: 'regex_flags' only valid for regex rules")

        # Validate fix object (optional)
        fix = None
        if 'fix' in rule:
            fix_obj = rule['fix']
            if not isinstance(fix_obj, dict):
                raise ValueError(f"Rule {rule['id']}: 'fix' must be an object")
            if 'kind' not in fix_obj:
                raise ValueError(f"Rule {rule['id']}: 'fix' must have 'kind'")
            if fix_obj['kind'] != 'replace':
                raise ValueError(f"Rule {rule['id']}: 'fix.kind' must be 'replace'")
            if 'template' not in fix_obj or not isinstance(fix_obj['template'], str):
                raise ValueError(f"Rule {rule['id']}: 'fix' must have a string 'template'")
            fix = {
                'kind': 'replace',
                'template': fix_obj['template']
            }

        validated_rule = {
            'id': rule['id'],
            'kind': rule['kind'],
            'languages': languages,
            'regex_flags': regex_flags if rule['kind'] == 'regex' else []
        }

        if rule['kind'] == 'selector':
            validated_rule['selector'] = rule['selector']
        else:
            validated_rule['pattern'] = rule['pattern']

        if fix:
            validated_rule['fix'] = fix

        validated_rules.append(validated_rule)

    return validated_rules


def get_source_files(root_dir: str) -> List[Tuple[str, str]]:
    """Get all source files under root_dir with their languages, sorted lexicographically."""
    files = []
    root_path = Path(root_dir)

    for path in root_path.rglob('*'):
        if path.is_file():
            ext = path.suffix.lower()
            if ext in FILE_EXTENSION_MAP:
                rel_path = path.relative_to(root_path)
                files.append((rel_path.as_posix(), FILE_EXTENSION_MAP[ext]))

    return sorted(files, key=lambda x: x[0])


def compile_regex_flags(flags: List[str]) -> int:
    """Convert flag strings to re module flags."""
    flag_map = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL}
    result = 0
    for flag in flags:
        result |= flag_map.get(flag, 0)
    return result


def find_exact_matches(content: str, pattern: str, file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all exact matches in content."""
    matches = []
    lines = content.split('\n')

    for line_num, line in enumerate(lines, start=1):
        col = 1
        while True:
            idx = line.find(pattern, col - 1)
            if idx == -1:
                break

            start_col = idx + 1  # 1-based
            end_col = start_col + len(pattern)

            matches.append({
                'rule_id': None,
                'file': file_path,
                'language': language,
                'start': {'line': line_num, 'col': start_col},
                'end': {'line': line_num, 'col': end_col},
                'match': pattern
            })
            col = start_col + 1

    return matches


def find_regex_matches(content: str, pattern: str, flags: List[str], file_path: str, language: str) -> List[Dict[str, Any]]:
    """Find all regex matches in content with proper line/column tracking."""
    matches = []
    re_flags = compile_regex_flags(flags)

    try:
        compiled = re.compile(pattern, re_flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}")

    lines = content.split('\n')

    line_offsets = [0]
    for line in lines[:-1]:
        line_offsets.append(line_offsets[-1] + len(line) + 1)

    for match in compiled.finditer(content):
        match_text = match.group(0)
        start_pos = match.start()
        end_pos = match.end()

        # Find line number for start position
        start_line = 1
        for i, offset in enumerate(line_offsets):
            if offset <= start_pos:
                start_line = i + 1
            else:
                break

        # Calculate start column
        start_col = start_pos - line_offsets[start_line - 1] + 1

        # Find line number for end position
        end_line = 1
        for i, offset in enumerate(line_offsets):
            if offset < end_pos:
                end_line = i + 1
            else:
                break

        # Calculate end column
        if end_line <= len(line_offsets):
            end_col = end_pos - line_offsets[end_line - 1] + 1
        else:
            # End is at the very end of content
            end_col = len(lines[-1]) + 1 if lines else 1
            end_line = len(lines)

        matches.append({
            'rule_id': None,
            'file': file_path,
            'language': language,
            'start': {'line': start_line, 'col': start_col},
            'end': {'line': end_line, 'col': end_col},
            'match': match_text
        })

    return matches


def extract_metavariables(pattern: str) -> Tuple[str, Dict[str, bool]]:
    """
    Extract metavariables from pattern and replace them with valid identifiers.
    Returns (transformed_pattern, metavar_info) where metavar_info maps name -> is_optional
    """
    metavar_info: Dict[str, bool] = {}

    # Replace $$ with a placeholder first
    placeholder = "__DOLLAR_PLACEHOLDER__"
    pattern = pattern.replace("$$", placeholder)

    meta_pattern = r'\$([A-Z_][A-Z0-9_]*)(\?)?'

    counter = [0]

    def replace_meta(match):
        name = match.group(1)
        is_optional = match.group(2) == '?'
        full_name = f"${name}"

        if full_name not in metavar_info:
            metavar_info[full_name] = is_optional
            counter[0] += 1
        elif metavar_info[full_name] != is_optional:
            # Same metavariable with different optional status - use the optional one
            metavar_info[full_name] = True

        # Generate a valid identifier for the language
        return f"__META_{counter[0]}__"

    transformed = re.sub(meta_pattern, replace_meta, pattern)

    # Restore $$ placeholders
    transformed = transformed.replace(placeholder, "$")

    return transformed, metavar_info


def get_text_from_node(source: bytes, node) -> str:
    """Get the text content of a node."""
    return source[node.start_byte:node.end_byte].decode('utf-8')


def get_position(source: bytes, byte_offset: int) -> Tuple[int, int]:
    """Convert byte offset to (line, col) where line and col are 1-based."""
    text = source[:byte_offset].decode('utf-8')
    lines = text.split('\n')
    line = len(lines)
    col = len(lines[-1]) + 1  # 1-based column
    return line, col


def get_node_text_and_range(source: bytes, node) -> Tuple[str, Dict[str, Any]]:
    """Get the text and range dict for a node using tree-sitter's point info."""
    text = get_text_from_node(source, node)
    # tree-sitter provides 0-based row/column via start_point and end_point
    start_point = node.start_point
    end_point = node.end_point
    # Convert to 1-based line and column
    start_line = start_point.row + 1
    start_col = start_point.column + 1
    end_line = end_point.row + 1
    end_col = end_point.column + 1
    return text, {
        'start': {'line': start_line, 'col': start_col},
        'end': {'line': end_line, 'col': end_col}
    }


class PatternMatcher:
    """Matches pattern rules against source code using tree-sitter AST."""

    def __init__(self, pattern: str, language: str):
        self.original_pattern = pattern
        self.language = language
        self.transformed_pattern, self.metavar_info = extract_metavariables(pattern)
        self.parser = get_parser(language)

        # Parse the pattern to get its AST
        pattern_bytes = self.transformed_pattern.encode('utf-8')
        self.pattern_tree = self.parser.parse(pattern_bytes)
        self.pattern_root = self.pattern_tree.root_node

        # Get the significant child from the pattern root
        # (skip wrapper nodes like module/expression_statement if the pattern has only one meaningful child)
        self.pattern_node = self._get_significant_pattern_node(self.pattern_root)

    def _get_significant_pattern_node(self, node):
        """Extract the significant child from a wrapper node like module."""
        wrapper_types = ('module', 'program', 'translation_unit', 'source_file',
                         'compilation_unit', 'haskell', 'declarations', 'top_splice',
                         'header')

        if node.type in wrapper_types and len(node.children) >= 1:
            child = node.children[0]
            # If the child is an expression_statement with a single child, use that
            if child.type == 'expression_statement' and len(child.children) == 1:
                return child.children[0]
            # Recursively unwrap
            return self._get_significant_pattern_node(child)
        return node

    def find_matches(self, source: bytes) -> List[Dict[str, Any]]:
        """Find all matches in the source code."""
        tree = self.parser.parse(source)
        root = tree.root_node

        matches = []
        self._match_node(root, source, matches)
        return matches

    def _match_node(self, node, source: bytes, matches: List[Dict[str, Any]]):
        """Recursively try to match pattern against node and its children."""
        bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

        if self._try_match(node, self.pattern_node, source, bindings):
            # Found a match - build the match result
            match_text, match_range = get_node_text_and_range(source, node)

            captures = {}
            for meta_name in sorted(bindings):
                texts_and_ranges = bindings[meta_name]
                # All occurrences must have the same text
                first_text = texts_and_ranges[0][0]
                ranges = [tr[1] for tr in texts_and_ranges]

                captures[meta_name] = {
                    'text': first_text,
                    'ranges': ranges
                }

            matches.append({
                'match': match_text,
                'range': match_range,
                'captures': captures
            })

        # Recurse into children
        for child in node.children:
            self._match_node(child, source, matches)

    def _try_match(self, source_node, pattern_node, source: bytes,
                   bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> bool:
        """
        Try to match pattern_node against source_node.
        Returns True if match succeeds, False otherwise.
        bindings is updated with metavariable bindings.
        """
        # Check if this is a metavariable placeholder
        pattern_text = get_text_from_node(self.transformed_pattern.encode('utf-8'), pattern_node)

        # Check for metavariable placeholder
        meta_match = re.match(r'^__META_(\d+)__$', pattern_text)
        if meta_match:
            # This is a metavariable
            meta_num = int(meta_match.group(1))
            # Find the original metavariable name
            meta_names = list(self.metavar_info.keys())
            if meta_num > 0 and meta_num <= len(meta_names):
                meta_name = meta_names[meta_num - 1]
            else:
                meta_name = None

            if meta_name is None:
                return False

            # Get the source text and range
            source_text, source_range = get_node_text_and_range(source, source_node)

            # Check if this metavariable was already bound
            if meta_name in bindings:
                # All occurrences must match the same text
                if bindings[meta_name][0][0] != source_text:
                    return False
                # Add this occurrence
                bindings[meta_name].append((source_text, source_range))
                return True
            else:
                # New binding
                bindings[meta_name] = [(source_text, source_range)]
                return True

        # Not a metavariable - check node types match
        if source_node.type != pattern_node.type:
            return False

        # Get children
        source_children = list(source_node.children)
        pattern_children = list(pattern_node.children)

        # Try to match children with support for metavariables matching multiple siblings
        return self._match_children(source_children, pattern_children, source, bindings)

    def _match_children(self, source_children, pattern_children, source: bytes,
                       bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> bool:
        """
        Match pattern children against source children.
        Supports metavariables matching multiple consecutive siblings.
        """
        if not pattern_children:
            return True

        if not source_children:
            return False

        # Use a greedy matching approach with backtracking
        return self._match_children_recursive(source_children, pattern_children, source, bindings, 0, 0, {})

    def _match_children_recursive(self, source_children, pattern_children, source: bytes,
                                  bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]],
                                  si: int, pi: int,
                                  temp_bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> bool:
        """
        Recursively match children with support for metavariables spanning multiple nodes.
        """
        # Skip trivia in source first
        while si < len(source_children) and self._is_trivia(source_children[si]):
            si += 1

        # Skip trivia in pattern
        while pi < len(pattern_children) and self._is_trivia(pattern_children[pi]):
            pi += 1

        # Base cases
        if pi >= len(pattern_children):
            # All pattern children matched
            if si >= len(source_children):
                # All source children consumed - success
                # Commit temp bindings
                for name, values in temp_bindings.items():
                    if name in bindings:
                        bindings[name].extend(values)
                    else:
                        bindings[name] = values
                return True
            else:
                # Extra source children - but this might be OK for some nodes
                # Only succeed if all remaining source children are punctuation/trivia
                remaining = source_children[si:]
                return all(self._is_trivia(child) for child in remaining)

        if si >= len(source_children):
            # No more source children but pattern children remain
            return False

        pattern_child = pattern_children[pi]
        pattern_child_text = get_text_from_node(self.transformed_pattern.encode('utf-8'), pattern_child)

        # Check if pattern child is a metavariable
        meta_match = re.match(r'^__META_(\d+)__$', pattern_child_text)

        if meta_match:
            # Metavariable - try to match 1 or more source children
            meta_num = int(meta_match.group(1))
            meta_names = list(self.metavar_info.keys())
            meta_name = meta_names[meta_num - 1] if 0 < meta_num <= len(meta_names) else None

            if meta_name is None:
                return False

            # Try different spans of source children (but stop at trivia/next pattern match)
            max_span = len(source_children) - si

            for span in range(1, max_span + 1):
                # Don't include trivia at the end of a metavariable match
                matched_children = source_children[si:si + span]
                if span > 1 and self._is_trivia(matched_children[-1]):
                    continue

                combined_text = b''.join(source[c.start_byte:c.end_byte] for c in matched_children).decode('utf-8')
                combined_range = {
                    'start': {'line': matched_children[0].start_point.row + 1,
                              'col': matched_children[0].start_point.column + 1},
                    'end': {'line': matched_children[-1].end_point.row + 1,
                            'col': matched_children[-1].end_point.column + 1}
                }

                # Check if this metavariable was already bound
                if meta_name in bindings:
                    if bindings[meta_name][0][0] != combined_text:
                        continue
                    new_temp = dict(temp_bindings)
                    new_temp.setdefault(meta_name, []).append((combined_text, combined_range))
                elif meta_name in temp_bindings:
                    if temp_bindings[meta_name][0][0] != combined_text:
                        continue
                    new_temp = dict(temp_bindings)
                    new_temp[meta_name] = list(temp_bindings[meta_name])
                    new_temp[meta_name].append((combined_text, combined_range))
                else:
                    new_temp = dict(temp_bindings)
                    new_temp[meta_name] = [(combined_text, combined_range)]

                # Try to match rest
                if self._match_children_recursive(source_children, pattern_children, source,
                                                  bindings, si + span, pi + 1, new_temp):
                    return True

            return False
        else:
            # Not a metavariable - must match exactly one source child
            source_child = source_children[si]

            # Try to match this one source child with this one pattern child
            new_bindings = dict(temp_bindings)
            if self._try_match_single(source_child, pattern_child, source, new_bindings):
                if self._match_children_recursive(source_children, pattern_children, source,
                                                  bindings, si + 1, pi + 1, new_bindings):
                    return True

            return False

    def _try_match_single(self, source_node, pattern_node, source: bytes,
                          bindings: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> bool:
        """
        Try to match a single source node against a single pattern node.
        Similar to original _try_match but doesn't handle multi-node metavariables.
        """
        pattern_text = get_text_from_node(self.transformed_pattern.encode('utf-8'), pattern_node)

        # Check for metavariable placeholder
        meta_match = re.match(r'^__META_(\d+)__$', pattern_text)
        if meta_match:
            meta_num = int(meta_match.group(1))
            meta_names = list(self.metavar_info.keys())
            meta_name = meta_names[meta_num - 1] if 0 < meta_num <= len(meta_names) else None

            if meta_name is None:
                return False

            source_text, source_range = get_node_text_and_range(source, source_node)

            if meta_name in bindings:
                if bindings[meta_name][0][0] != source_text:
                    return False
                bindings[meta_name].append((source_text, source_range))
            else:
                bindings[meta_name] = [(source_text, source_range)]
            return True

        # Not a metavariable - check node types match
        if source_node.type != pattern_node.type:
            return False

        # Get children and match them
        source_children = list(source_node.children)
        pattern_children = list(pattern_node.children)

        # Check text content for leaf nodes
        if len(pattern_children) == 0:
            source_text = get_text_from_node(source, source_node)
            if source_text != pattern_text:
                return False
            return True

        # Match children
        return self._match_children(source_children, pattern_children, source, bindings)

    def _is_trivia(self, node) -> bool:
        """Check if a node is trivia/punctuation that can be skipped."""
        # Common trivia types
        trivia_types = {
            '(', ')', '[', ']', '{', '}', ',', ';', ':', '.',
            'comment', 'line_comment', 'block_comment',
        }
        return node.type in trivia_types or (not node.children and len(node.type) <= 2)


def find_pattern_matches(content: str, pattern: str, language: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all pattern matches in content using tree-sitter."""
    try:
        matcher = PatternMatcher(pattern, language)
        source = content.encode('utf-8')
        raw_matches = matcher.find_matches(source)

        matches = []
        for raw in raw_matches:
            match = {
                'rule_id': None,
                'file': file_path,
                'language': language,
                'start': raw['range']['start'],
                'end': raw['range']['end'],
                'match': raw['match'],
            }
            if raw['captures']:
                match['captures'] = raw['captures']
            matches.append(match)

        return matches
    except Exception as e:
        # If pattern parsing fails, return no matches
        return []


def find_selector_matches(content: str, selector: str, language: str, file_path: str) -> List[Dict[str, Any]]:
    """Find all AST nodes matching the selector type."""
    try:
        parser = get_parser(language)
        source = content.encode('utf-8')
        tree = parser.parse(source)
        root = tree.root_node

        # Get the language-specific node types for this selector
        node_types = SELECTOR_NODE_TYPE_MAP.get(language, {}).get(selector, ())
        if not node_types:
            return []

        matches = []
        _find_nodes_by_type(root, source, node_types, file_path, language, matches)
        return matches
    except Exception:
        return []


def _find_nodes_by_type(node, source: bytes, node_types: Tuple[str, ...],
                        file_path: str, language: str, matches: List[Dict[str, Any]]):
    """Recursively find all nodes matching the given types."""
    if node.type in node_types:
        text, range_info = get_node_text_and_range(source, node)
        matches.append({
            'rule_id': None,
            'file': file_path,
            'language': language,
            'start': range_info['start'],
            'end': range_info['end'],
            'match': text
        })

    for child in node.children:
        _find_nodes_by_type(child, source, node_types, file_path, language, matches)


def expand_template(template: str, match_text: str, captures: Optional[Dict[str, Any]] = None) -> str:
    """
    Expand a template string by replacing placeholders.
    $NAME is replaced by the text of the capture with that name.
    $MATCH is replaced by the full matched text.
    $$ is replaced by a literal $.
    """
    if captures is None:
        captures = {}

    result = []
    i = 0
    while i < len(template):
        if template[i] == '$':
            if i + 1 < len(template):
                if template[i + 1] == '$':
                    # Escaped $
                    result.append('$')
                    i += 2
                elif template[i + 1] == 'M':
                    # Check for $MATCH
                    if template[i:i+6] == '$MATCH':
                        result.append(match_text)
                        i += 6
                    else:
                        # Look for a capture name
                        j = i + 1
                        while j < len(template) and (template[j].isalnum() or template[j] == '_'):
                            j += 1
                        name = template[i:j]
                        if name in captures:
                            result.append(captures[name]['text'])
                            i = j
                        else:
                            result.append(template[i])
                            i += 1
                else:
                    # Look for a capture name starting with uppercase letter
                    j = i + 1
                    while j < len(template) and (template[j].isalnum() or template[j] == '_'):
                        j += 1
                    name = template[i:j]
                    if name in captures:
                        result.append(captures[name]['text'])
                        i = j
                    else:
                        result.append(template[i])
                        i += 1
            else:
                result.append(template[i])
                i += 1
        else:
            result.append(template[i])
            i += 1

    return ''.join(result)


def ranges_overlap(range1: Dict[str, Any], range2: Dict[str, Any]) -> bool:
    """Check if two ranges overlap."""
    start1 = (range1['start']['line'], range1['start']['col'])
    end1 = (range1['end']['line'], range1['end']['col'])
    start2 = (range2['start']['line'], range2['start']['col'])
    end2 = (range2['end']['line'], range2['end']['col'])

    return not (end1 <= start2 or end2 <= start1)


def search_file(file_path: str, full_path: str, rules: List[Dict[str, Any]],
                encoding: str, language: str) -> List[Dict[str, Any]]:
    """Search a single file for all rule matches."""
    try:
        with open(full_path, 'r', encoding=encoding) as f:
            content = f.read()
    except (UnicodeDecodeError, IOError):
        return []

    all_matches = []

    for rule in rules:
        # Skip rules that don't apply to this language
        if language not in rule['languages']:
            continue

        if rule['kind'] == 'exact':
            matches = find_exact_matches(content, rule['pattern'], file_path, language)
        elif rule['kind'] == 'regex':
            matches = find_regex_matches(content, rule['pattern'], rule['regex_flags'], file_path, language)
        elif rule['kind'] == 'selector':
            matches = find_selector_matches(content, rule['selector'], language, file_path)
        else:  # pattern
            matches = find_pattern_matches(content, rule['pattern'], language, file_path)

        for match in matches:
            match['rule_id'] = rule['id']

        all_matches.extend(matches)

    return all_matches


def sort_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort matches by file, then start position, then end position, then rule_id.
    For same start position: earlier end first, then rule_id.
    """
    return sorted(matches, key=lambda m: (
        m['file'],
        m['start']['line'],
        m['start']['col'],
        m['end']['line'],
        m['end']['col'],
        m['rule_id']
    ))


def sort_key(item: Dict[str, Any]) -> Tuple:
    """
    Generate sort key for output items.
    Sort by: file, start.line, start.col, rule_id.
    Match lines come before fix lines when keys are equal.
    """
    is_fix = item.get('event') == 'fix'
    return (
        item['file'],
        item['start']['line'],
        item['start']['col'],
        item['rule_id'],
        0 if not is_fix else 1  # match (0) before fix (1)
    )


def sort_output_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort all output items (matches and fixes) by the normalization rules."""
    return sorted(items, key=sort_key)


def build_fix_line(match: Dict[str, Any], rule: Dict[str, Any],
                   applied: bool, skipped_reason: Optional[str] = None) -> Dict[str, Any]:
    """Build a fix output line from a match and its rule."""
    template = rule['fix']['template']
    captures = match.get('captures')
    replacement = expand_template(template, match['match'], captures)

    return {
        'event': 'fix',
        'rule_id': rule['id'],
        'file': match['file'],
        'language': match['language'],
        'start': match['start'],
        'end': match['end'],
        'replacement': replacement,
        'applied': applied,
        'skipped_reason': skipped_reason
    }


def apply_fixes_to_file(content: str, fixes: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> str:
    """Apply a list of fixes to file content, sorted by position."""
    # Sort fixes by position (earliest first)
    fixes_sorted = sorted(fixes, key=lambda x: (
        x[0]['start']['line'],
        x[0]['start']['col']
    ))

    lines = content.split('\n')

    # Apply fixes in reverse order (from end to start) to preserve positions
    # But we need to track overlaps, so we'll process from start to end
    # and track which positions have been modified

    # Convert line-based positions to character offsets
    line_offsets = [0]
    for line in lines[:-1]:
        line_offsets.append(line_offsets[-1] + len(line) + 1)  # +1 for newline

    # Build list of (start_byte, end_byte, replacement) for applied fixes
    byte_fixes = []
    applied_ranges = []

    for match, fix_info in fixes_sorted:
        # Calculate byte positions
        start_line = match['start']['line']
        start_col = match['start']['col']
        end_line = match['end']['line']
        end_col = match['end']['col']

        # Validate line numbers
        if start_line > len(lines) or end_line > len(lines):
            continue

        # Calculate start and end byte positions
        start_byte = line_offsets[start_line - 1] + start_col - 1
        end_byte = line_offsets[end_line - 1] + end_col - 1

        # Check if this fix overlaps any previously applied fix
        current_range = {'start': match['start'], 'end': match['end']}
        overlaps = False
        for prev_range in applied_ranges:
            if ranges_overlap(current_range, prev_range):
                overlaps = True
                break

        if not overlaps:
            byte_fixes.append((start_byte, end_byte, fix_info['replacement'], match, fix_info))
            applied_ranges.append(current_range)

    # Sort byte fixes by start position (reverse order for applying)
    byte_fixes.sort(key=lambda x: x[0], reverse=True)

    # Apply fixes
    result = content
    applied_fixes = []
    skipped_fixes = []

    for start_byte, end_byte, replacement, match, fix_info in byte_fixes:
        result = result[:start_byte] + replacement + result[end_byte:]
        applied_fixes.append((match, fix_info))

    # Track skipped fixes (those that would have overlapped)
    all_fixes_set = set((m['start']['line'], m['start']['col'], m['rule_id']) for m, _ in fixes_sorted)
    applied_fixes_set = set((m['start']['line'], m['start']['col'], m['rule_id']) for m, _ in applied_fixes)

    for match, fix_info in fixes_sorted:
        key = (match['start']['line'], match['start']['col'], match['rule_id'])
        if key not in applied_fixes_set:
            skipped_fixes.append((match, fix_info, 'overlap'))

    return result, applied_fixes, skipped_fixes


def main():
    """Main entry point."""
    args = parse_args()

    # Load rules
    try:
        rules = load_rules(args.rules)
    except Exception as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    # Get all source files
    try:
        source_files = get_source_files(args.root_dir)
    except Exception as e:
        print(f"Error scanning directory: {e}", file=sys.stderr)
        sys.exit(1)

    # Build a rule lookup by ID
    rule_by_id = {rule['id']: rule for rule in rules}

    # Search each file and collect matches
    all_matches = []
    for rel_file, language in source_files:
        full_path = os.path.join(args.root_dir, rel_file)
        matches = search_file(rel_file, full_path, rules, args.encoding, language)
        all_matches.extend(matches)

    # Sort matches
    sorted_matches = sort_matches(all_matches)

    # Determine if we need to process fixes
    has_fix_mode = args.dry_run or args.apply_fixes

    if not has_fix_mode:
        # Just output matches without fixes
        for match in sorted_matches:
            # Ensure captures keys are sorted when serializing
            if 'captures' in match:
                match['captures'] = dict(sorted(match['captures'].items()))
            print(json.dumps(match, separators=(',', ': ')))
    else:
        # Process fixes
        # Group matches by file for fix application
        matches_by_file: Dict[str, List[Dict[str, Any]]] = {}
        for match in sorted_matches:
            file = match['file']
            if file not in matches_by_file:
                matches_by_file[file] = []
            matches_by_file[file].append(match)

        # Collect all output items (matches and fixes)
        all_output_items = []

        # Process each file
        file_contents: Dict[str, str] = {}
        file_fixes_to_apply: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}

        for file, file_matches in matches_by_file.items():
            full_path = os.path.join(args.root_dir, file)

            # Read file content
            try:
                with open(full_path, 'r', encoding=args.encoding) as f:
                    content = f.read()
                file_contents[file] = content
            except (UnicodeDecodeError, IOError):
                file_contents[file] = None

            # Collect fixes for this file
            file_fixes = []
            for match in file_matches:
                rule = rule_by_id[match['rule_id']]
                if 'fix' in rule:
                    fix_line = build_fix_line(match, rule, applied=False)
                    file_fixes.append((match, fix_line))

            file_fixes_to_apply[file] = file_fixes

        if args.dry_run:
            # Just output matches and fix previews (not applied)
            for match in sorted_matches:
                output_item = {
                    'rule_id': match['rule_id'],
                    'file': match['file'],
                    'language': match['language'],
                    'start': match['start'],
                    'end': match['end'],
                    'match': match['match']
                }
                if 'captures' in match:
                    output_item['captures'] = dict(sorted(match['captures'].items()))
                all_output_items.append(output_item)

                # Add fix preview
                rule = rule_by_id[match['rule_id']]
                if 'fix' in rule:
                    fix_line = build_fix_line(match, rule, applied=False)
                    all_output_items.append(fix_line)

            # Sort and output
            sorted_output = sort_output_items(all_output_items)
            for item in sorted_output:
                print(json.dumps(item, separators=(',', ': ')))

        elif args.apply_fixes:
            # Apply fixes and track results
            # First, collect all matches for output
            for match in sorted_matches:
                output_item = {
                    'rule_id': match['rule_id'],
                    'file': match['file'],
                    'language': match['language'],
                    'start': match['start'],
                    'end': match['end'],
                    'match': match['match']
                }
                if 'captures' in match:
                    output_item['captures'] = dict(sorted(match['captures'].items()))
                all_output_items.append(output_item)

            # Apply fixes to files
            applied_fix_results = []
            skipped_fix_results = []

            for file, file_fixes in file_fixes_to_apply.items():
                if not file_fixes or file_contents.get(file) is None:
                    continue

                content = file_contents[file]
                new_content, applied, skipped = apply_fixes_to_file(content, file_fixes)

                # Write the modified content
                full_path = os.path.join(args.root_dir, file)
                with open(full_path, 'w', encoding=args.encoding) as f:
                    f.write(new_content)

                # Track results
                for match, fix_info in applied:
                    rule = rule_by_id[match['rule_id']]
                    fix_line = build_fix_line(match, rule, applied=True)
                    applied_fix_results.append(fix_line)

                for match, fix_info, reason in skipped:
                    rule = rule_by_id[match['rule_id']]
                    fix_line = build_fix_line(match, rule, applied=False, skipped_reason=reason)
                    skipped_fix_results.append(fix_line)

            # Add all fix results to output
            all_output_items.extend(applied_fix_results)
            all_output_items.extend(skipped_fix_results)

            # Sort and output
            sorted_output = sort_output_items(all_output_items)
            for item in sorted_output:
                print(json.dumps(item, separators=(',', ': ')))

    sys.exit(0)


if __name__ == '__main__':
    main()
