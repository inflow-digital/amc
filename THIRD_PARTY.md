# Third-party software

AMC's source code does not include third-party code. When installed, it depends on the
packages below (versions as locked in `uv.lock` at the time of writing; licenses from each
package's metadata). All are permissive except `certifi` (MPL-2.0, weak file-level
copyleft: it is used unmodified, as a separate package). No GPL/LGPL/AGPL packages are
used at runtime.

AMC's built-in executor keeps tool and argument names compatible with
[DesktopCommanderMCP](https://github.com/wonderwhy-er/DesktopCommanderMCP) (MIT); no
DesktopCommanderMCP code is included (see `NOTICE`).

## Direct runtime dependencies

| Package | Version | License |
|---|---|---|
| [mcp](https://github.com/modelcontextprotocol/python-sdk) | 1.30.0 | MIT |
| [fastapi](https://github.com/fastapi/fastapi) | 0.142.2 | MIT |
| [pydantic](https://github.com/pydantic/pydantic) | 2.13.5 | MIT |
| [uvicorn](https://github.com/encode/uvicorn) (with `[standard]` extras) | 0.54.0 | BSD-3-Clause |
| [websockets](https://github.com/python-websockets/websockets) | 17.1 | BSD-3-Clause |
| [httpx](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause |

## Transitive runtime dependencies

| Package | Version | License | Pulled in by |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | fastapi |
| annotated-types | 0.8.0 | MIT | pydantic |
| anyio | 4.15.1 | MIT | httpx, mcp, starlette |
| attrs | 26.1.0 | MIT | jsonschema |
| certifi | 2026.7.22 | **MPL-2.0** | httpx |
| cffi | 2.1.1 | MIT-0 | cryptography |
| click | 8.5.0 | BSD-3-Clause | uvicorn |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | pyjwt |
| h11 | 0.16.0 | MIT | httpcore, uvicorn |
| httpcore | 1.0.9 | BSD-3-Clause | httpx |
| httptools | 0.8.0 | MIT | uvicorn[standard] |
| httpx-sse | 0.4.3 | MIT | mcp |
| idna | 3.20 | BSD-3-Clause | anyio, httpx |
| jsonschema | 4.26.0 | MIT | mcp |
| jsonschema-specifications | 2025.9.1 | MIT | jsonschema |
| opentelemetry-api | 1.45.0 | Apache-2.0 | mcp |
| pycparser | 3.0 | BSD-3-Clause | cffi |
| pydantic-core | 2.46.5 | MIT | pydantic |
| pydantic-settings | 2.15.0 | MIT | mcp |
| pyjwt | 2.15.1 | MIT | mcp |
| python-dotenv | 1.2.3 | BSD-3-Clause | pydantic-settings, uvicorn[standard] |
| python-multipart | 0.0.32 | Apache-2.0 | mcp |
| pywin32 (Windows only) | 312 | PSF-2.0 | mcp |
| pyyaml | 6.0.3 | MIT | uvicorn[standard] |
| referencing | 0.37.0 | MIT | jsonschema |
| rpds-py | 2026.6.3 | MIT | referencing |
| sse-starlette | 3.5.0 | BSD-3-Clause | mcp |
| starlette | 1.7.0 | BSD-3-Clause | fastapi, mcp |
| typing-extensions | 4.16.0 | PSF-2.0 | several |
| typing-inspection | 0.4.4 | MIT | pydantic |
| uvloop (not on Windows) | 0.22.1 | MIT OR Apache-2.0 | uvicorn[standard] |
| watchfiles | 1.3.0 | MIT | uvicorn[standard] |

Development-only tools (pytest, pytest-asyncio, ruff) are not shipped.

To regenerate this list from an environment:

```sh
uv run python -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version, d.metadata.get('License-Expression') or d.metadata.get('License')) for d in m.distributions()]"
```
