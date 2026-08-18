import os
import psycopg # Imports the PostgreSQL library that lets Python connect to Postgres and run SQL.  
from dotenv import load_dotenv # Imports a helper that reads variables from your .env file.

load_dotenv()  # Load environment variables from .env file

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing — check your .env")

def get_connection():
    """Open a new connection to the database."""
    return psycopg.connect(DATABASE_URL)

# Given an event code like "ideate2026", 
# check the database and return the event name if it exists.
def get_event(event_code: str) -> str | None:
    """Look up a hackathon by its code.

    Returns the event's display name, or None if no such event exists.
    """
    # What it needs to do: open a connection, run a SELECT for the name where 
    # event_code matches, and return the name — or None if nothing was found.

    with get_connection() as conn: # connect to Supabase/Postgres.
        with conn.cursor() as cur: # create a tool called cur that can send SQL queries through that connection.
            cur.execute("SELECT name FROM events WHERE event_code = %s", (event_code,)) # Hey database, run this SQL
            result = cur.fetchone() # Give me the first row you found
            if result:
                return result[0]  # Return the name
            else:
                return None  # No such event exists


if __name__ == "__main__":
    print(get_event("ideate2026"))   # should print: IDEATE 2026
    print(get_event("nonsense"))     # should print: None
