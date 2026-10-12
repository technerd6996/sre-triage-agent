import os
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()
uri, user, pw = os.getenv("NEO4J_URI"), os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASS")
print("URI:", uri, "| user:", user, "| password set:", bool(pw))

driver = GraphDatabase.driver(uri, auth=(user, pw))
driver.verify_connectivity()
print("Connected")
with driver.session() as s:
    print("Edges:", s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"])
driver.close()