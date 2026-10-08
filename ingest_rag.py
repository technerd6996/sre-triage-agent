import os
import csv
from dotenv import load_dotenv
from neo4j import GraphDatabase

# Load secrets from .env file
load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS")

EDGES_FILE = os.path.join(os.path.dirname(__file__), "graph_edges_core.csv")


def load_edges(path: str):
    """Reads (src, rel, dst) rows from the CSV file."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [(r["src"].strip(), r["rel"].strip(), r["dst"].strip())
                for r in reader if r["src"] and r["rel"] and r["dst"]]


def seed_neo4j_graph():
    """Migrates the topology and knowledge graph into Neo4j AuraDB."""
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    edges = load_edges(EDGES_FILE)

    # Same schema the app uses: (:Entity {name})-[:RELATED_TO {type}]->(:Entity)
    query = """
    UNWIND $rows AS row
    MERGE (src:Entity {name: row.src})
    MERGE (dst:Entity {name: row.dst})
    MERGE (src)-[r:RELATED_TO {type: row.rel}]->(dst)
    """
    rows = [{"src": s, "rel": r, "dst": d} for s, r, d in edges]

    with driver.session() as session:
        try:
            session.run("CREATE CONSTRAINT entity_name IF NOT EXISTS "
                        "FOR (e:Entity) REQUIRE e.name IS UNIQUE")
        except Exception as e:
            print(f"Constraint skipped: {e}")

        for i in range(0, len(rows), 100):
            session.run(query, rows=rows[i:i + 100])
            print(f"Loaded {min(i + 100, len(rows))}/{len(rows)} edges")

    driver.close()


if __name__ == "__main__":
    print("Seeding Neo4j AuraDB Knowledge Graph...")
    seed_neo4j_graph()
    print("Complete.")