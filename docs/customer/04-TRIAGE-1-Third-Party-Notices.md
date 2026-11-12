# TRIAGE-1 Third-Party Software Notices

**TRIAGE-1 version 1.0.0**

TRIAGE-1 includes the third-party software listed below. Each component is licensed to you under its own licence, not under the TRIAGE-1 End User Licence Agreement, and nothing in that agreement limits your rights under these licences. The full licence text of each Python library is included in the product, inside that library's `.dist-info` folder under `Lib\site-packages` (Windows) or `/usr/local/lib/python3.13/site-packages` (Docker).

## 1. Runtime and database

| Component | Version | Licence | Source |
|---|---|---|---|
| Python | 3.13.16 | PSF-2.0 | https://www.python.org/downloads/source/ |
| PostgreSQL (Windows installer, bundled server and tools) | 17.11 | PostgreSQL License | https://www.postgresql.org/ftp/source/ |
| WinSW, Windows service wrapper (Windows installer) | 2.12.0 | MIT | https://github.com/winsw/winsw |

The Python and PostgreSQL licence texts are included as `PYTHON-LICENSE.txt` and `POSTGRESQL-LICENSE.txt` in the installation folder.

## 2. Components under the GNU Lesser General Public License

| Component | Version | Licence |
|---|---|---|
| ldap3 | 2.9.1 | LGPL-3.0 |
| paramiko | 5.0.0 | LGPL-2.1 |
| psycopg, psycopg-binary | 3.3.6 | LGPL-3.0 |
| psycopg-pool | 3.3.3 | LGPL-3.0 |

These libraries are shipped **unmodified, as separate files**, and TRIAGE-1 loads them at run time. You may:

- replace them with your own modified versions of the same interface;
- reverse engineer TRIAGE-1 only as far as needed to debug such modifications, as the EULA permits.

You can get the source code of the exact versions shipped from the project sites listed in Section 4. For **three years** from the date you received this version, [Vendor legal name] will also supply it on request, at no more than the cost of distribution, from [contact email].

## 3. Docker image

The Docker image is based on the official `python:3.13-slim` image, built on Debian 13 ("trixie"), and adds the PostgreSQL 17 client tools from the PostgreSQL Debian repository. Those operating-system packages are each licensed under their own terms, which include the GNU General Public License for some packages. Their copyright and licence files are inside the image under `/usr/share/doc/<package>/copyright`.

- Source code for the Debian packages is available from https://sources.debian.org and https://snapshot.debian.org.
- Source code for the PostgreSQL client packages is available from https://apt.postgresql.org.
- For three years from the date you received this version, [Vendor legal name] will also supply the corresponding source code on request from [contact email].

## 4. Python libraries

| Library | Version | Licence | Project |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio |
| bcrypt | 4.0.1 | Apache-2.0 | https://github.com/pyca/bcrypt/ |
| boto3 | 1.43.114 | Apache-2.0 | https://github.com/boto/boto3 |
| botocore | 1.43.114 | Apache-2.0 | https://github.com/boto/botocore |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| charset-normalizer | 3.5.2 | MIT | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click/ |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography/ |
| distro | 1.9.0 | Apache-2.0 | https://github.com/python-distro/distro |
| fastapi | 0.143.0 | MIT | https://github.com/fastapi/fastapi |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://github.com/encode/httpcore |
| httpcore2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2/blob/main/src/httpcore2 |
| httpx | 0.28.1 | BSD | https://github.com/encode/httpx |
| httpx2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| hvac | 2.4.0 | Apache-2.0 | https://github.com/hvac/hvac |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| invoke | 3.0.3 | BSD-2-Clause | https://github.com/pyinvoke/invoke |
| jiter | 0.17.0 | MIT | https://github.com/pydantic/jiter/ |
| jmespath | 1.1.0 | MIT | https://github.com/jmespath/jmespath.py |
| jsonpatch | 1.35 | BSD-3-Clause | https://github.com/stefankoegl/python-json-patch |
| jsonpointer | 3.2.1 | BSD | https://github.com/stefankoegl/python-json-pointer |
| langchain | 1.4.5 | MIT | https://docs.langchain.com/ |
| langchain-core | 1.6.10 | MIT | https://docs.langchain.com/ |
| langchain-openai | 1.7.1 | MIT | https://docs.langchain.com/oss/python/integrations/providers/openai |
| langchain-protocol | 0.0.19 | MIT | https://github.com/langchain-ai/agent-protocol/tree/main/streaming |
| langgraph | 1.2.15 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/langgraph |
| langgraph-checkpoint | 4.3.0 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint |
| langgraph-checkpoint-postgres | 3.2.0 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres |
| langgraph-prebuilt | 1.1.0 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/prebuilt |
| langgraph-sdk | 0.4.7 | MIT | https://github.com/langchain-ai/langgraph/tree/main/libs/sdk-py |
| langsmith | 0.14.8 | MIT | https://smith.langchain.com/ |
| ldap3 | 2.9.1 | LGPL-3.0 | https://github.com/cannatag/ldap3 |
| openai | 3.29.0 | Apache-2.0 | https://github.com/openai/openai-python |
| opentelemetry-api | 1.45.1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api |
| opentelemetry-sdk | 1.45.1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-sdk |
| opentelemetry-semantic-conventions | 0.66b1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-semantic-conventions |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | https://github.com/ijl/orjson |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT | https://github.com/ormsgpack/ormsgpack |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| paramiko | 5.0.0 | LGPL-2.1 | https://github.com/paramiko/paramiko |
| passlib | 1.7.4 | BSD | https://passlib.readthedocs.io |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil |
| psycopg | 3.3.6 | LGPL-3.0 | https://psycopg.org/ |
| psycopg-binary | 3.3.6 | LGPL-3.0 | https://psycopg.org/ |
| psycopg-pool | 3.3.3 | LGPL-3.0 | https://psycopg.org/ |
| pyasn1 | 0.6.4 | BSD-2-Clause | https://github.com/pyasn1/pyasn1 |
| pycparser | 3.11 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pycryptodome | 4.2.0 | BSD-2-Clause AND Unlicense AND MIT | https://github.com/Legrandin/pycryptodome/ |
| pydantic | 2.14.1 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.50.1 | MIT | https://github.com/pydantic/pydantic/tree/main/pydantic-core |
| pynacl | 1.6.2 | Apache-2.0 | https://github.com/pyca/pynacl/ |
| pyspnego | 0.12.4 | MIT | https://github.com/jborean93/pyspnego |
| python-dateutil | 2.9.0.post0 | BSD-3-Clause OR Apache-2.0 | https://github.com/dateutil/dateutil |
| python-dotenv | 1.2.4 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| python-multipart | 0.0.32 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| pywinrm | 0.5.0 | MIT | http://github.com/diyan/pywinrm/ |
| pyyaml | 6.0.3 | MIT | https://github.com/yaml/pyyaml |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python | https://github.com/mrabarnett/mrab-regex |
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| requests-toolbelt | 1.0.0 | Apache-2.0 | https://github.com/requests/toolbelt |
| requests_ntlm | 1.3.0 | ISC | https://github.com/requests/requests-ntlm |
| s3transfer | 0.19.2 | Apache-2.0 | https://github.com/boto/s3transfer |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | https://github.com/python-trio/sniffio |
| sspilib | 0.6.0 | MIT | https://github.com/jborean93/sspilib |
| starlette | 1.7.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| tenacity | 9.2.1 | Apache-2.0 | https://github.com/jd/tenacity |
| tiktoken | 0.14.0 | MIT | https://github.com/openai/tiktoken |
| truststore | 0.10.5 | MIT | https://github.com/sethmlarson/truststore |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| tzdata | 2026.5 | Apache-2.0 | https://github.com/python/tzdata |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| uuid_utils | 0.17.1 | BSD-3-Clause | https://github.com/aminalaee/uuid-utils |
| uvicorn | 0.54.0 | BSD-3-Clause | https://github.com/Kludex/uvicorn |
| websockets | 15.0.1 | BSD | https://tidelift.com/subscription/pkg/pypi-websockets?utm_source=pypi-websockets&utm_medium=referral&utm_campaign=readme |
| xmltodict | 1.0.4 | MIT | https://github.com/martinblech/xmltodict |
| xxhash | 4.0.1 | BSD-2-Clause | https://github.com/ifduyue/python-xxhash |
| zstandard | 0.25.0 | BSD-3-Clause | https://github.com/indygreg/python-zstandard |

## 5. Licence summary

| Licence | What it requires of you | Components |
|---|---|---|
| MIT, BSD, ISC, Apache-2.0, PSF-2.0, PostgreSQL | Keep the copyright and licence notices when you redistribute | Most libraries, Python, PostgreSQL, WinSW |
| MPL-2.0 | Changes to those specific files must be shared under MPL-2.0 if you distribute them | certifi, part of orjson |
| LGPL-2.1, LGPL-3.0 | The library must stay replaceable, and its source must be available | ldap3, paramiko, psycopg |
| GPL (some Debian packages in the Docker image only) | Source code must be available | Base operating-system packages |

Your normal use of TRIAGE-1 inside your organisation does not create redistribution obligations. The table above matters if you pass the software on to others.
