#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry shim; the implementation lives in `agent_gateway.cli.minod`."""

import sys
from pathlib import Path

from agent_gateway.cli.minod import main

if __name__ == "__main__":
    sys.exit(main(scripts_dir=Path(__file__).resolve().parent))
