# encoding:utf-8

import os,re
from bot import bot_factory
from bridge.bridge import Bridge
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_message import ChatMessage
from config import conf
import plugins
from plugins import *
from common.log import logger
from common import const
import sqlite3

@plugins.register(name="Summary", desire_priority=-1, desc="A simple plugin to summary messages", version="0.1", author="lanvent")
class Summary(Plugin):
    def __init__(self):
        super().__init__()
        
        curdir = os.path.dirname(__file__)
        db_path = os.path.join(curdir, "chat.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chat_records
                    (sessionid TEXT, msgid INTEGER, user TEXT, content TEXT, type TEXT, timestamp INTEGER,
                    PRIMARY KEY (sessionid, msgid))''')
        self.conn.commit()

        btype = Bridge().btype['chat']
        if btype not in [const.OPEN_AI, const.CHATGPT, const.CHATGPTONAZURE]:
            raise Exception("[Summary] init failed, not supported bot type")
        self.bot = bot_factory.create_bot(Bridge().btype['chat'])
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
        logger.info("[Summary] inited")

    def _insert_record(self, session_id, msg_id, user, content, msg_type, timestamp):
        c = self.conn.cursor()
        logger.debug("[Summary] insert record: {} {} {} {} {} {}" .format(session_id, msg_id, user, content, msg_type, timestamp))
        c.execute("INSERT INTO chat_records VALUES (?,?,?,?,?,?)", (session_id, msg_id, user, content, msg_type, timestamp))
        self.conn.commit()
    
    def _get_records(self, session_id, start_date=0, limit=9999):
        c = self.conn.cursor()
        c.execute("SELECT * FROM chat_records WHERE sessionid=? and timestamp>? ORDER BY timestamp DESC LIMIT ?", (session_id, start_date, limit))
        return c.fetchall()

    def on_receive_message(self, e_context: EventContext):
        context = e_context['context']
        cmsg : ChatMessage = e_context['context']['msg']
        username = None
        session_id = cmsg.from_user_id
        if conf().get('channel_type', 'wx') == 'wx' and cmsg.from_user_nickname is not None:
            session_id = cmsg.from_user_nickname # itchat channel id会变动，只好用群名作为session id

        if context["isgroup"]:
            username = cmsg.actual_user_nickname
            if username is None:
                username = cmsg.actual_user_id
        else:
            username = cmsg.from_user_nickname
            if username is None:
                username = cmsg.from_user_id
        self._insert_record(session_id, cmsg.msg_id, username, context.content, str(context.type), cmsg.create_time)
        # logger.debug("[Summary] {}:{} ({})" .format(username, context.content, session_id))

    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type != ContextType.TEXT:
            return
        
        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        if content == trigger_prefix+"总结":
            msg:ChatMessage = e_context['context']['msg']
            session_id = msg.from_user_id
            if conf().get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
                session_id = msg.from_user_nickname # itchat channel id会变动，只好用名字作为session id
            records = self._get_records(session_id, 0)
            for i in range(len(records)):
                record=list(records[i])
                content = record[3]
                clist = re.split(r'\n- - - - - - - - -.*?\n', content)
                if len(clist) > 1:
                    record[3] = clist[1]
                    records[i] = tuple(record)
            if len(records) <= 1:
                reply = Reply(ReplyType.INFO, "当前无聊天记录")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            max_tokens = 3600
            sessions = self.bot.sessions

            def check(records):
                query = ""
                for record in records[::-1]:
                    username = record[2]
                    content = record[3]
                    if record[4] in [str(ContextType.IMAGE),str(ContextType.VOICE)]:
                        content = f"[{record[4]}]"    
                    query += f'"{username}"' + ": " + content + "\n\n"
                prompt = "你是一位群聊机器人，你需要对给出的聊天记录进行摘要，要求简明扼要，以包含列表的大纲形式输出，如果识别到说话人情绪请使用emoji表情表示。\n在聊天记录中，[xxx]表示对图片或声音文件的说明。\n"
                if e_context['context']['isgroup']:
                    prefixs = conf().get('group_chat_prefix',[''])
                else:
                    prefixs = conf().get('single_chat_prefix',[''])
                if len(prefixs) > 0:
                    prompt += "{"+",".join([f'"{prefix}"' for prefix in prefixs])+"}" + "里的词语是在聊天中触发你回复的前缀，你的回复不会包含在聊天记录中。\n"
                plugin_trigger_prefix = conf().get('plugin_trigger_prefix', "$")
                prompt += f"在触发你回复后，剩下的内容如果以{plugin_trigger_prefix}开始，表示需要触发额外安装的插件功能（你无法感知到）。\n"
                session = sessions.build_session(session_id, prompt)

                session.add_query("现在需要你总结如下聊天记录：\n\n%s"%query)
                if  session.calc_tokens() > max_tokens:
                    # logger.debug("[Summary] summary failed, tokens: %d" % session.calc_tokens())
                    return None
                return session

            session = check(records)
            if session is None:
                left,right = 0, len(records)
                while left < right:
                    mid = (left + right) // 2
                    logger.debug("[Summary] left: %d, right: %d, mid: %d" % (left, right, mid))
                    session = check(records[:mid])
                    if session is None:
                        right = mid - 1
                    else:
                        left = mid + 1
                session = check(records[:right])
                logger.debug("[Summary] summary %d messages" % (right))
            else:
                logger.debug("[Summary] summary all %d messages" % (len(records)))
            logger.debug("[Summary] session query: %s, prompt_tokens: %d" % (session.messages, session.calc_tokens()))
            result = self.bot.reply_text(session)
            total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], result['content']
            logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (total_tokens, completion_tokens, reply_content))
            if completion_tokens == 0:
                reply = Reply(ReplyType.ERROR, reply_content)
            else:
                reply = Reply(ReplyType.TEXT, reply_content)
                
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS # 事件结束，并跳过处理context的默认逻辑


    def get_help_text(self, verbose = False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入{trigger_prefix}总结，我会帮助你总结聊天记录。\n"
        return help_text
