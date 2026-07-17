-- Allow 'imports' edges in the reference graph, for code-import/dependency
-- relationships between source files (kept in parity with the local SQLite
-- schema in shared/sqlite_schema.sql). Hosted code ingestion is not wired up
-- yet; this only widens the enum so the graph model matches.
ALTER TABLE document_references
    DROP CONSTRAINT document_references_reference_type_check;
ALTER TABLE document_references
    ADD CONSTRAINT document_references_reference_type_check
    CHECK (reference_type IN ('cites', 'links_to', 'imports'));
