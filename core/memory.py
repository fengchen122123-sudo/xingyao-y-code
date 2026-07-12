import sqlite3

# 全局撤销状态
LAST_UNDO_STATE = {
    "file_path": None,
    "old_content": None
}

def set_undo_state(file_path, old_content):
    LAST_UNDO_STATE["file_path"] = file_path
    LAST_UNDO_STATE["old_content"] = old_content

def get_undo_state():
    return LAST_UNDO_STATE

class MemoryManager:
    def __init__(self, db_path="ycode.db"):
        # 1. 连接数据库
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        # 2. 立刻建表
        self.create_tables()

    def create_tables(self):
        # 3. 准备执行 SQL 命令
        cursor = self.conn.cursor()
        
        # 4. 创建一个叫 sessions 的表（相当于 Excel 里的一个工作表）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT UNIQUE
            )
        ''')
        
        # 5. 创建一个叫 messages 的表（用来存具体的聊天记录）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                role TEXT,
                content TEXT
            )
        ''')
        # 6. 保存修改
        self.conn.commit()

    def add_message(self, session_name, role, content):
        """记笔记技能：把聊天内容存进数据库"""
        cursor = self.conn.cursor()
        
        # 1. 档案袋处理：如果这个会话名字不存在，就新建一个；如果有了，就忽略 (IGNORE)
        cursor.execute('''
            INSERT OR IGNORE INTO sessions (session_name)
            VALUES (?)
        ''', (session_name,))
        
        # 2. 查出这个档案袋的专属 ID 号码
        cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
        session_id = cursor.fetchone()[0]
        
        import json
        if isinstance(content, (list, dict)):
            content = json.dumps(content, ensure_ascii=False)
        else:
            content = str(content)
            
        # 3. 把角色和说的内容存进 messages 表，并贴上档案袋的 ID 标签
        cursor.execute('''
            INSERT INTO messages (session_id, role, content)
            VALUES (?, ?, ?)
        ''', (session_id, role, content))
        
        # 4. 提交保存到硬盘/内存
        self.conn.commit()

    def get_history(self, session_name):
        """翻旧账技能：根据会话名字，把历史记录全拿出来"""
        cursor = self.conn.cursor()
        
        # 1. 先去查查有没有这个档案袋
        cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
        row = cursor.fetchone()
        
        # 如果连档案袋都找不到，说明是新聊天，直接返回空列表 []
        if row is None:
            return []
            
        session_id = row[0]
        
        # 2. 根据档案袋 ID，把所有的聊天记录按时间顺序（id 从小到大）查出来
        cursor.execute('''
            SELECT role, content FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
        ''', (session_id,))
        
        # 3. 把查出来的数据，打包成 Python 认识的格式（字典列表）
        import json
        history = []
        for record in cursor.fetchall():
            role = record[0]
            raw_content = record[1]
            try:
                parsed = json.loads(raw_content)
                history.append({"role": role, "content": parsed})
            except (ValueError, TypeError):
                history.append({"role": role, "content": raw_content})
            
        return history

    def undo_last_interaction(self, session_name):
        """抹除大模型最近一次回答的记忆（防止大模型误以为文件没被回退）"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
        row = cursor.fetchone()
        if not row: return
        session_id = row[0]
        
        # 删除倒数两条记录（大模型的思考回答 + 相关的 tool_call 记录，甚至是 user 的指令）
        # 这里为了简单，直接删除该 session_id 下的最后几条非 user 记录，直到遇到上一个 user
        # 但其实也可以直接删掉 id 最大的几条，我们保留最后一次 user 提问，删掉后面的 assistant 和 tool
        
        # 找到最后一条 user 记录的 ID
        cursor.execute('''
            SELECT id FROM messages 
            WHERE session_id = ? AND role = 'user' 
            ORDER BY id DESC LIMIT 1
        ''', (session_id,))
        last_user = cursor.fetchone()
        
        if last_user:
            user_msg_id = last_user[0]
            # 删除这条 user 提问【之后】产生的所有 assistant 和 tool 记录
            cursor.execute('''
                DELETE FROM messages
                WHERE session_id = ? AND id > ?
            ''', (session_id, user_msg_id))
            self.conn.commit()

    def get_all_sessions(self):
        """返回所有会话的列表，包含名称和消息数量"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT s.session_name, COUNT(m.id) as msg_count
            FROM sessions s
            LEFT JOIN messages m ON s.id = m.session_id
            GROUP BY s.id
            ORDER BY s.id DESC
        ''')
        return [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]
        
    def delete_session(self, session_name):
        """删除指定会话及其所有消息"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
        row = cursor.fetchone()
        if row:
            session_id = row[0]
            cursor.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
            cursor.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
            self.conn.commit()

    def prune_history_files(self, session_name, max_turns=16, retain_recent=4):
        """物理级折叠历史挂载的大文件，以节省 token。"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
        row = cursor.fetchone()
        if not row: return
        session_id = row[0]
        
        cursor.execute('SELECT id, role, content FROM messages WHERE session_id = ? ORDER BY id ASC', (session_id,))
        records = cursor.fetchall()
        
        if len(records) <= max_turns:
            return
            
        import json
        import re
        
        for i in range(len(records) - retain_recent):
            msg_id, role, raw_content = records[i]
            if role == "user":
                try:
                    content_obj = json.loads(raw_content)
                    modified = False
                    
                    def prune_text(text):
                        if "[附带上下文:" in text:
                            return re.sub(
                                r"(\[附带上下文: .*?\])\n```.*?\n```\n?", 
                                r"\1 (⚠️ 内容已折叠以节省 Token。若需再次查看请使用 read_file 工具)\n\n", 
                                text, 
                                flags=re.DOTALL
                            )
                        return text

                    if isinstance(content_obj, list):
                        for block in content_obj:
                            if block.get("type") == "text":
                                old_text = block.get("text", "")
                                new_text = prune_text(old_text)
                                if new_text != old_text:
                                    block["text"] = new_text
                                    modified = True
                    elif isinstance(content_obj, str):
                        new_text = prune_text(content_obj)
                        if new_text != content_obj:
                            content_obj = new_text
                            modified = True
                            
                    if modified:
                        cursor.execute('UPDATE messages SET content = ? WHERE id = ?', (json.dumps(content_obj, ensure_ascii=False), msg_id))
                except Exception:
                    pass
        self.conn.commit()