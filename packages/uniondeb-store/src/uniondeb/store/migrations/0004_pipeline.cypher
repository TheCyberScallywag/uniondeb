// Ingest errors, mapping proposals, jobs, registries
CREATE CONSTRAINT ingest_error_id IF NOT EXISTS FOR (n:IngestError) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT mapping_proposal_id IF NOT EXISTS FOR (n:MappingProposal) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT alias_registry_version IF NOT EXISTS FOR (n:AliasRegistry) REQUIRE n.version IS UNIQUE;
CREATE CONSTRAINT extract_contract_hash IF NOT EXISTS FOR (n:ExtractContract) REQUIRE n.hash IS UNIQUE;
CREATE CONSTRAINT trigger_list_version IF NOT EXISTS FOR (n:TriggerList) REQUIRE n.version IS UNIQUE;
CREATE CONSTRAINT job_id IF NOT EXISTS FOR (n:Job) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT reviewer_id IF NOT EXISTS FOR (n:Reviewer) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT eval_report_id IF NOT EXISTS FOR (n:EvalReport) REQUIRE n.id IS UNIQUE;
CREATE INDEX job_state IF NOT EXISTS FOR (n:Job) ON (n.state);
CREATE INDEX alert_state IF NOT EXISTS FOR (n:Alert) ON (n.state);
CREATE INDEX review_item_kind IF NOT EXISTS FOR (n:ReviewItem) ON (n.kind);
CREATE INDEX person_person_id IF NOT EXISTS FOR (n:Person) ON (n.person_id);
