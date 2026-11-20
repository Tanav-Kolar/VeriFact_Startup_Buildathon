CREATE TABLE GraphNode (
  event_id     STRING(64)  NOT NULL,
  node_id      STRING(256) NOT NULL,   -- item_id, account_id, outlet_id, storyline_id, claim_id, or event_id
  label        STRING(32)  NOT NULL,   -- Event | Post | Account | Article | Outlet | Storyline | Claim
  valid_from   TIMESTAMP   NOT NULL,   -- created_at of the item that made it appear
  platform     STRING(16),
  storyline_id STRING(64),
  properties   JSON,
  updated_at   TIMESTAMP   NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (event_id, node_id);

CREATE TABLE GraphEdge (
  event_id   STRING(64)  NOT NULL,
  node_id    STRING(256) NOT NULL,     -- source node
  edge_label STRING(32)  NOT NULL,     -- REPLIED_TO | QUOTED | LINKS_TO | AUTHORED_BY | PUBLISHED_BY | BELONGS_TO | SUPPORTS | CONTRADICTS
  dest_id    STRING(256) NOT NULL,     -- destination node
  valid_from TIMESTAMP   NOT NULL,
  properties JSON,
  updated_at TIMESTAMP   NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (event_id, node_id, edge_label, dest_id),
  INTERLEAVE IN PARENT GraphNode ON DELETE CASCADE;

CREATE INDEX GraphNodeByTime ON GraphNode (event_id, valid_from);
CREATE INDEX GraphNodeByStoryline ON GraphNode (event_id, storyline_id, valid_from);
CREATE INDEX GraphEdgeByDest ON GraphEdge (event_id, dest_id);

CREATE PROPERTY GRAPH verifact_graph
  NODE TABLES (
    GraphNode KEY (event_id, node_id)
      DYNAMIC LABEL (label)
      DYNAMIC PROPERTIES (properties)
  )
  EDGE TABLES (
    GraphEdge KEY (event_id, node_id, edge_label, dest_id)
      SOURCE KEY (event_id, node_id) REFERENCES GraphNode (event_id, node_id)
      DESTINATION KEY (event_id, dest_id) REFERENCES GraphNode (event_id, node_id)
      DYNAMIC LABEL (edge_label)
      DYNAMIC PROPERTIES (properties)
  );
