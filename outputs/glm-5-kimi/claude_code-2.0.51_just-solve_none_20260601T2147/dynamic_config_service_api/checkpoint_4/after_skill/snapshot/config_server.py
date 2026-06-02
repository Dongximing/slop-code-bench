#!/usr/bin/env python3
"""Config Server - Configuration management with schema registry, workflow, and policy guardrails.

This module re-exports from the modularized components for backward compatibility.
"""

from parsers import parse_raw_config, ParseError, normalize_value, canonical_json
from schema_validation import (
    SchemaValidationError, validate_config_against_schema,
    check_external_refs, get_json_type
)
from json_utils import (
    get_value_by_pointer, compute_diffs, canonical_json as _canonical_json,
    deep_merge
)
from stores import (
    SchemaRegistry, ConfigStore, ProposalStore,
    PolicyBundleStore, PolicyBindingStore
)
from rego_engine import RegoEngine, RegoParser
from server import run_server, main, ConfigServerHandler

__all__ = [
    'parse_raw_config', 'ParseError', 'normalize_value', 'canonical_json',
    'SchemaValidationError', 'validate_config_against_schema',
    'check_external_refs', 'get_json_type',
    'get_value_by_pointer', 'compute_diffs', 'deep_merge',
    'SchemaRegistry', 'ConfigStore', 'ProposalStore',
    'PolicyBundleStore', 'PolicyBindingStore',
    'RegoEngine', 'RegoParser',
    'run_server', 'main', 'ConfigServerHandler',
]

if __name__ == "__main__":
    main()
