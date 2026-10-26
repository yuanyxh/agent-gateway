#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry shim; the implementation lives in `agent_gateway.services.knowledge.knowledge_refill`."""

from agent_gateway.services.knowledge.knowledge_refill import main

if __name__ == "__main__":
    raise SystemExit(main())
