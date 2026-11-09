#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry shim; the implementation lives in `agent_gateway.scanner.ast_extractor`."""

# ruff: noqa: E402 - cache isolation must run before agent_gateway imports

try:
    from _eval_env import isolate_cache
except ModuleNotFoundError:
    from scripts._eval_env import isolate_cache

isolate_cache(__file__)

from agent_gateway.scanner.ast_extractor import main

if __name__ == "__main__":
    main()
