"""
┌──────────────────────────────────────────────────────────────────────────────┐
│ @author: Davidson Gomes                                                      │
│ @file: database.py                                                           │
│ Developed by: Davidson Gomes                                                 │
│ Creation date: May 13, 2025                                                  │
│ Contact: contato@evolution-api.com                                           │
├──────────────────────────────────────────────────────────────────────────────┤
│ @copyright © Evolution API 2025. All rights reserved.                        │
│ Licensed under the Apache License, Version 2.0                               │
│                                                                              │
│ You may not use this file except in compliance with the License.             │
│ You may obtain a copy of the License at                                      │
│                                                                              │
│    http://www.apache.org/licenses/LICENSE-2.0                                │
│                                                                              │
│ Unless required by applicable law or agreed to in writing, software          │
│ distributed under the License is distributed on an "AS IS" BASIS,            │
│ WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.     │
│ See the License for the specific language governing permissions and          │
│ limitations under the License.                                               │
├──────────────────────────────────────────────────────────────────────────────┤
│ @important                                                                   │
│ For any future changes to the code in this file, it is recommended to        │
│ include, together with the modification, the information of the developer    │
│ who changed it and the date of modification.                                 │
└──────────────────────────────────────────────────────────────────────────────┘
"""

from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from src.config.settings import settings
from src.config.database_url import (
    configure_pg8000_keepalive,
    get_sync_database_config,
)

POSTGRES_CONNECTION_STRING = settings.POSTGRES_CONNECTION_STRING
SYNC_DATABASE_URL, SYNC_DATABASE_CONNECT_ARGS = get_sync_database_config(
    POSTGRES_CONNECTION_STRING
)

# pool_pre_ping revalidates a pooled connection before handing it out, so a
# connection dropped by the server (restart, idle timeout) is recycled instead
# of surfacing as a 500 on the next request.
#
# TCP keepalives keep the socket — and its
# Docker Swarm/IPVS conntrack entry, which is reaped after ~15 min idle — alive
# so a connection held across a slow operation is not silently dropped mid-query.
# pre_ping only guards the checkout boundary; keepalives guard an already
# checked-out connection.
engine = create_engine(
    SYNC_DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args=SYNC_DATABASE_CONNECT_ARGS,
)
event.listen(engine, "connect", configure_pg8000_keepalive)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
