import os
import sys

# Test estimate_tokens
from core.llm import estimate_tokens
messages = [{"role": "user", "content": "hello world"}]
tokens = estimate_tokens("gpt-4o-mini", messages)
print("Estimated tokens:", tokens)

# Test manager.prune_history_files changes
from core.memory import MemoryManager
manager = MemoryManager()
session = "test_token_prune"
manager.conn.execute("INSERT OR IGNORE INTO sessions (session_name) VALUES (?)", (session,))
manager.conn.commit()

manager.add_message(session, "user", "very long user content "*100)
history = manager.get_history(session)
initial_tokens = estimate_tokens("gpt-4o-mini", history)
print("Initial tokens:", initial_tokens)

# Fake background worker logic
if initial_tokens > 100:  # Fake threshold
    print("Triggering prune!")
    manager.prune_history_files(session, max_turns=0, retain_recent=0)
    history = manager.get_history(session)
    final_tokens = estimate_tokens("gpt-4o-mini", history)
    print("Final tokens after prune:", final_tokens)

manager.conn.execute("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE session_name=?)", (session,))
manager.conn.execute("DELETE FROM sessions WHERE session_name=?", (session,))
manager.conn.commit()

print("Tests completed successfully.")
