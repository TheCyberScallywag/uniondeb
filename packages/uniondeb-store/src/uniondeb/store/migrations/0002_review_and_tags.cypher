// Implements: UND-013 — append-only tags, review queue and alerts (plan §4, §6)
CREATE CONSTRAINT tag_id IF NOT EXISTS FOR (n:Tag) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT review_item_id IF NOT EXISTS FOR (n:ReviewItem) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT alert_id IF NOT EXISTS FOR (n:Alert) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT extract_id IF NOT EXISTS FOR (n:Extract) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT case_pack_id IF NOT EXISTS FOR (n:CasePack) REQUIRE n.id IS UNIQUE;
CREATE INDEX tag_current_label IF NOT EXISTS FOR (n:Tag) ON (n.current, n.label);
CREATE INDEX record_dataset IF NOT EXISTS FOR (n:Record) ON (n.dataset);
CREATE INDEX record_occurred_at IF NOT EXISTS FOR (n:Record) ON (n.occurred_at);
CREATE INDEX review_item_state IF NOT EXISTS FOR (n:ReviewItem) ON (n.state);
