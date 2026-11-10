"""FROZEN snapshot of the three Drive tables exactly as the application created them BEFORE Phase 3A.2-B (generated with SQLAlchemy `create_all` from the
models at commit 8f7e66b, SQLite dialect). Migration tests load it to rebuild a database in the shape every existing installation has, then
migrate that database. NEVER regenerate it from the current models: its whole purpose is to stay what the old code produced."""

LEGACY_DRIVE_DDL = """
CREATE TABLE document_storage (
	id INTEGER NOT NULL,
	kind VARCHAR(20) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	original_filename VARCHAR(500) NOT NULL,
	size INTEGER NOT NULL,
	md5 VARCHAR(32) NOT NULL,
	location VARCHAR(200) NOT NULL,
	state VARCHAR(20) NOT NULL,
	last_error VARCHAR(500),
	attempts INTEGER NOT NULL,
	document_kind VARCHAR(10),
	document_pk INTEGER,
	created_at DATETIME NOT NULL,
	created_by_id INTEGER,
	stored_at DATETIME,
	archived_at DATETIME,
	archived_by_id INTEGER,
	restored_at DATETIME,
	restored_by_id INTEGER,
	missing_detected_at DATETIME,
	last_verified_at DATETIME,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_document_storage_kind_sha256 UNIQUE (kind, sha256),
	FOREIGN KEY(created_by_id) REFERENCES users (id),
	FOREIGN KEY(archived_by_id) REFERENCES users (id),
	FOREIGN KEY(restored_by_id) REFERENCES users (id)
);
CREATE TABLE drive_sync_state (
	name VARCHAR(40) NOT NULL,
	page_token VARCHAR(200),
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (name)
);
CREATE TABLE storage_events (
	id INTEGER NOT NULL,
	record_id INTEGER,
	event_type VARCHAR(60) NOT NULL,
	detail VARCHAR(1000),
	actor_id INTEGER,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(record_id) REFERENCES document_storage (id),
	FOREIGN KEY(actor_id) REFERENCES users (id)
);
CREATE INDEX ix_document_storage_document_pk ON document_storage (document_pk);
CREATE INDEX ix_document_storage_kind ON document_storage (kind);
CREATE INDEX ix_document_storage_sha256 ON document_storage (sha256);
CREATE INDEX ix_document_storage_state ON document_storage (state);
CREATE INDEX ix_storage_events_event_type ON storage_events (event_type);
CREATE INDEX ix_storage_events_record_id ON storage_events (record_id);
"""

LEGACY_DOCUMENT_STORAGE_COLUMNS = (
    "id", "kind", "sha256", "original_filename", "size", "md5", "location", "state", "last_error", "attempts", "document_kind", "document_pk", "created_at",
    "created_by_id", "stored_at", "archived_at", "archived_by_id", "restored_at", "restored_by_id", "missing_detected_at", "last_verified_at", "updated_at",
)
