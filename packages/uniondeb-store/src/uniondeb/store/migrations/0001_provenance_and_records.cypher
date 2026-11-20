// Implements: UND-013 — system-of-record identity constraints (plan §4)
CREATE CONSTRAINT source_file_sha256 IF NOT EXISTS FOR (n:SourceFile) REQUIRE n.sha256 IS UNIQUE;
CREATE CONSTRAINT record_key IF NOT EXISTS FOR (n:Record) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT device_id IF NOT EXISTS FOR (n:Device) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT identifier_kind_value IF NOT EXISTS FOR (n:Identifier) REQUIRE (n.kind, n.value) IS NODE KEY;
CREATE CONSTRAINT person_id IF NOT EXISTS FOR (n:Person) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT email_message_id IF NOT EXISTS FOR (n:Email) REQUIRE n.message_id IS UNIQUE;
CREATE CONSTRAINT email_address IF NOT EXISTS FOR (n:EmailAddress) REQUIRE n.address IS UNIQUE;
CREATE CONSTRAINT attachment_sha256 IF NOT EXISTS FOR (n:Attachment) REQUIRE n.sha256 IS UNIQUE;
CREATE CONSTRAINT activity_id IF NOT EXISTS FOR (n:Activity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT software_agent_name_version IF NOT EXISTS FOR (n:SoftwareAgent) REQUIRE (n.name, n.version) IS NODE KEY;
