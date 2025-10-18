from phi.agent import Agent
from phi.tools.sql import SQLTools
from phi.model.groq import Groq
import chainlit as cl
from dotenv import load_dotenv
import os
import sqlalchemy
from sqlalchemy import create_engine, text
import re

load_dotenv()
api_key = os.getenv('GROQ_API_KEY')


class CustomSQLTools:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.engine = create_engine(db_url)

    def get_schema_info(self):
        schema_info = " **Database Schema Information:**\n\n"

        try:
            with self.engine.connect() as conn:
                # Get all tables
                result = conn.execute(text("SHOW TABLES"))
                tables = [row[0] for row in result]

                schema_info += f"**Available Tables:** {', '.join(tables)}\n\n"

                # Get detailed info for each table
                for table in tables:
                    try:
                        # Get column information
                        desc_result = conn.execute(text(f"DESCRIBE {table}"))
                        columns = desc_result.fetchall()

                        schema_info += f"**Table: {table}**\n"
                        for col in columns:
                            field, type_info, null, key, default, extra = col
                            auto_increment = " (AUTO_INCREMENT)" if "auto_increment" in str(extra).lower() else ""
                            primary_key = " (PRIMARY KEY)" if "PRI" in str(key) else ""
                            schema_info += f"  - {field}: {type_info}{primary_key}{auto_increment}\n"
                        schema_info += "\n"

                    except Exception as e:
                        schema_info += f"  - Could not get details for table {table}: {str(e)}\n\n"

        except Exception as e:
            schema_info += f"Error getting schema: {str(e)}\n"
            # Fallback to basic table list
            try:
                with self.engine.connect() as conn:
                    result = conn.execute(text("SHOW TABLES"))
                    tables = [row[0] for row in result]
                    schema_info += f"Available tables: {', '.join(tables)}"
            except:
                schema_info += "Could not retrieve database information."

        return schema_info

    def execute_query(self, query: str):
        """Execute a query and return results - ACTUALLY modifies the database"""
        try:
            with self.engine.begin() as conn:  # Use begin() for auto-commit on success
                result = conn.execute(text(query))

                if query.strip().upper().startswith('SELECT'):
                    rows = result.fetchall()
                    columns = result.keys()
                    return {"columns": list(columns), "rows": [list(row) for row in rows]}
                else:
                    # For INSERT, UPDATE, DELETE - the transaction will auto-commit
                    rowcount = result.rowcount
                    return {"message": f"Query executed successfully. Rows affected: {rowcount}", "rowcount": rowcount}
        except Exception as e:
            # If there's an error, the transaction will auto-rollback
            raise Exception(f"Database execution failed: {str(e)}")


def create_agent(db_url: str, schema_info: str):
    # Create agent that generates queries with full schema awareness
    sql_agent = Agent(
        model=Groq(id="llama-3.3-70b-versatile", api_key=api_key),
        add_chat_history_to_messages=True,
        num_history_responses=3,
        description=f"""You are a helpful AI agent that generates SQL queries based on user questions about the database. 

DATABASE SCHEMA:
{schema_info}

IMPORTANT RULES:
- For INSERT queries, do NOT include AUTO_INCREMENT columns (they will be automatically generated)
- Always check the schema to understand which columns are required
- For tables with id columns that are AUTO_INCREMENT, exclude them from INSERT statements
- Pay attention to PRIMARY KEY and AUTO_INCREMENT columns
- Generate syntactically correct SQL for MySQL database""",
        instructions=[
            "Generate SQL queries based on user questions about the database.",
            "Do not execute queries - only provide the SQL code.",
            "Always explain what the query does in plain English.",
            "For INSERT queries, automatically exclude AUTO_INCREMENT columns from the column list and values.",
            "Check the database schema before generating queries to ensure correct column usage.",
            "If the user greets you, greet them back politely.",
            "For data modification queries (INSERT, UPDATE, DELETE), add a warning about potential data changes.",
            "Format your response with the SQL query clearly separated and explained.",
            "Make sure your SQL syntax is correct for MySQL database.",
            "For INSERT/UPDATE/DELETE queries, be very explicit about what data will be changed.",
            "Always consider column constraints, data types, and AUTO_INCREMENT fields."
        ]
    )
    return sql_agent


def extract_sql_query(response_text: str):
    """Extract SQL query from agent response"""
    # Look for SQL queries in various formats
    sql_patterns = [
        r'```sql\s*(.*?)\s*```',
        r'```\s*(SELECT.*?;)\s*```',
        r'```\s*(INSERT.*?;)\s*```',
        r'```\s*(UPDATE.*?;)\s*```',
        r'```\s*(DELETE.*?;)\s*```',
        r'```\s*(CREATE.*?;)\s*```',
        r'```\s*(ALTER.*?;)\s*```'
    ]

    for pattern in sql_patterns:
        matches = re.findall(pattern, response_text, re.DOTALL | re.IGNORECASE)
        if matches:
            return matches[0].strip()

    return None


def is_safe_query(query: str):
    """Check if query is safe to execute (basic safety check)"""
    query_upper = query.upper().strip()
    dangerous_keywords = ['DROP', 'TRUNCATE', 'DELETE FROM', 'ALTER TABLE']

    for keyword in dangerous_keywords:
        if keyword in query_upper:
            return False
    return True


def get_query_type(query: str):
    """Determine if query is read-only or modifies data"""
    query_upper = query.upper().strip()
    if query_upper.startswith('SELECT'):
        return 'READ'
    elif query_upper.startswith(('INSERT', 'UPDATE', 'DELETE')):
        return 'MODIFY'
    else:
        return 'OTHER'


async def execute_query_logic():
    """Common logic for executing queries - ACTUALLY modifies the database"""
    try:
        sql_tools = cl.user_session.get("sql_tools")
        pending_query = cl.user_session.get("pending_query")

        if not sql_tools or not pending_query:
            await cl.Message(content=" No query to execute.").send()
            return

        # Show what we're about to execute
        query_type = get_query_type(pending_query)
        await cl.Message(content=f" Executing query on your local database...\n```sql\n{pending_query}\n```").send()

        # ACTUALLY execute the query on the database
        result = sql_tools.execute_query(pending_query)

        if "columns" in result:  # SELECT query result
            columns = result["columns"]
            rows = result["rows"]

            if not rows:
                await cl.Message(content=" Query executed successfully on your database. No results found.").send()
            else:
                # Create a formatted table
                table_content = f" **Query Results from your database** ({len(rows)} rows):\n\n"
                table_content += "| " + " | ".join(columns) + " |\n"
                table_content += "|" + "|".join(["---" for _ in columns]) + "|\n"

                for row in rows[:10]:  # Limit to first 10 rows
                    table_content += "| " + " | ".join(
                        [str(cell) if cell is not None else "NULL" for cell in row]) + " |\n"

                if len(rows) > 10:
                    table_content += f"\n*Showing first 10 rows of {len(rows)} total results.*"

                await cl.Message(content=table_content).send()

        else:  # Modification query result (INSERT, UPDATE, DELETE)
            rowcount = result.get("rowcount", 0)
            await cl.Message(
                content=f" **Database Modified Successfully!**\n\n{result['message']}\n\n **Changes have been applied to your local database.**").send()

            # For modifications, offer to show a verification query
            if query_type == 'MODIFY':
                await cl.Message(
                    content=" Tip: You can ask me to 'show the updated data' to verify the changes.").send()

        # Clear the pending query
        cl.user_session.set("pending_query", None)

    except Exception as e:
        await cl.Message(
            content=f" **Database Error**: {str(e)}\n\nThe query was NOT executed. Your database remains unchanged.").send()
        cl.user_session.set("pending_query", None)


@cl.on_chat_start
async def on_chat_start():
    await cl.Message(
        content="Please enter your database URL (e.g., 'mysql+pymysql://<username>:<password>@<host>:<port>/<database>') to get started."
    ).send()
    cl.user_session.set("awaiting_db_url", True)


@cl.on_message
async def on_message(message: cl.Message):
    try:
        if cl.user_session.get("awaiting_db_url"):
            db_url = message.content.strip()
            cl.user_session.set("db_url", db_url)

            # Initialize custom SQL tools
            sql_tools = CustomSQLTools(db_url)
            cl.user_session.set("sql_tools", sql_tools)

            # Get detailed schema info
            schema_info = sql_tools.get_schema_info()

            # Create agent for query generation with schema awareness
            sql_agent = create_agent(db_url, schema_info)
            cl.user_session.set("agent", sql_agent)
            cl.user_session.set("awaiting_db_url", False)

            await cl.Message(
                content=f"Database connected successfully!\n\n{schema_info}\n\n🤖 I now understand your database structure and will generate appropriate SQL queries. You can ask me to add, update, delete, or retrieve data."
            ).send()
            return

        agent = cl.user_session.get("agent")
        sql_tools = cl.user_session.get("sql_tools")

        if not agent or not sql_tools:
            await cl.Message(content="Please connect to a database first.").send()
            return

        # Check if we're waiting for query confirmation
        if cl.user_session.get("awaiting_confirmation"):
            user_response = message.content.strip().lower()
            if user_response in ['execute', 'yes', 'y', 'run']:
                cl.user_session.set("awaiting_confirmation", False)
                await execute_query_logic()
                return
            elif user_response in ['cancel', 'no', 'n', 'skip']:
                cl.user_session.set("awaiting_confirmation", False)
                cl.user_session.set("pending_query", None)
                await cl.Message(content=" Query execution cancelled.").send()
                return
            else:
                await cl.Message(content="Please respond with 'execute' or 'cancel'.").send()
                return

        # Generate response with SQL query
        response = ""
        msg = cl.Message(content="")

        # For simplicity, get full response first (you can keep streaming if preferred)
        full_response = agent.run(message.content)
        response = full_response.content

        await cl.Message(content=response).send()

        # Extract SQL query from response
        sql_query = extract_sql_query(response)

        if sql_query:
            query_type = get_query_type(sql_query)
            is_safe = is_safe_query(sql_query)

            if not is_safe:
                await cl.Message(
                    content="This query contains potentially dangerous operations and cannot be executed for safety reasons."
                ).send()
                return

            # Store query in session for button callback
            cl.user_session.set("pending_query", sql_query)

            # Create action buttons - Try different formats for compatibility
            actions = []

            try:
                # Method 1: Standard format
                if query_type == 'READ':
                    actions.append(cl.Action(
                        name="execute_query",
                        value="execute",
                        label=" Execute Query"
                    ))
                elif query_type == 'MODIFY':
                    actions.append(cl.Action(
                        name="execute_query",
                        value="execute",
                        label=" Apply Changes"
                    ))
                else:
                    actions.append(cl.Action(
                        name="execute_query",
                        value="execute",
                        label=" Execute Query"
                    ))

                actions.append(cl.Action(
                    name="cancel_query",
                    value="cancel",
                    label=" Cancel"
                ))

            except Exception as action_error:
                print(f"Action creation failed: {action_error}")
                # Fallback: no actions, just instructions
                actions = None

            query_msg = f"**Generated SQL Query:**\n```sql\n{sql_query}\n```\n\n"
            if query_type == 'MODIFY':
                query_msg += " **Warning**: This query will modify your database. Please review carefully before executing.\n\n"
            else:
                query_msg += "This query will retrieve data from your database.\n\n"

            if actions:
                await cl.Message(content=query_msg, actions=actions).send()
            else:
                # Fallback to text instructions
                query_msg += "Reply with:\n- 'execute' or 'yes' to run the query\n- 'cancel' or 'no' to skip"
                await cl.Message(content=query_msg).send()
                cl.user_session.set("awaiting_confirmation", True)

    except Exception as e:
        await cl.Message(content=f"An error occurred: {str(e)}").send()


@cl.action_callback("execute_query")
async def execute_query_callback(action):
    await execute_query_logic()


@cl.action_callback("cancel_query")
async def cancel_query_callback(action):
    cl.user_session.set("pending_query", None)
    await cl.Message(content=" Query execution cancelled.").send()

# To run this app, use: chainlit run main3.py
# connection string
# mysql+pymysql://root:Ankush12-@localhost:3306/startersql