// Implements: UND-013 — cases, flags, SARs and the hash-chained audit log (plan §7)
CREATE CONSTRAINT case_id IF NOT EXISTS FOR (n:Case) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT flag_id IF NOT EXISTS FOR (n:Flag) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT case_note_id IF NOT EXISTS FOR (n:CaseNote) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT sar_draft_id IF NOT EXISTS FOR (n:SarDraft) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT sar_submission_id IF NOT EXISTS FOR (n:SarSubmission) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT audit_event_id IF NOT EXISTS FOR (n:AuditEvent) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT audit_event_seq IF NOT EXISTS FOR (n:AuditEvent) REQUIRE n.seq IS UNIQUE;
CREATE CONSTRAINT audit_event_hash IF NOT EXISTS FOR (n:AuditEvent) REQUIRE n.hash IS UNIQUE;
CREATE INDEX case_state IF NOT EXISTS FOR (n:Case) ON (n.state);
