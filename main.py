# encoding:utf-8

import os,re
from bot import bot_factory
from bridge.bridge import Bridge
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from config import conf
import plugins
from plugins import *
from common.log import logger
from common import const
import sqlite3

@plugins.register(name="Summary", desire_priority=-1, desc="A simple plugin to summary messages", version="0.2", author="lanvent")
class Summary(Plugin):
    def __init__(self):
        super().__init__()
        
        curdir = os.path.dirname(__file__)
        db_path = os.path.join(curdir, "chat.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS chat_records
                    (sessionid TEXT, msgid INTEGER, user TEXT, content TEXT, type TEXT, timestamp INTEGER, is_triggered INTEGER,
                    PRIMARY KEY (sessionid, msgid))''')
        
        # 后期增加了is_triggered字段，这里做个过渡，这段代码某天会删除
        c = c.execute("PRAGMA table_info(chat_records);")
        column_exists = False
        for column in c.fetchall():
            logger.debug("[Summary] column: {}" .format(column))
            if column[1] == 'is_triggered':
                column_exists = True
                break
        if not column_exists:
            self.conn.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0;")
            self.conn.execute("UPDATE chat_records SET is_triggered = 0;")

        self.conn.commit()

        btype = Bridge().btype['chat']
        if btype not in [const.OPEN_AI, const.CHATGPT, const.CHATGPTONAZURE]:
            raise Exception("[Summary] init failed, not supported bot type")
        self.bot = bot_factory.create_bot(Bridge().btype['chat'])
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
        logger.info("[Summary] inited")

    def _insert_record(self, session_id, msg_id, user, content, msg_type, timestamp, is_triggered = 0):
        c = self.conn.cursor()
        logger.debug("[Summary] insert record: {} {} {} {} {} {} {}" .format(session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
        c.execute("INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?)", (session_id, msg_id, user, content, msg_type, timestamp, is_triggered))
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

        if context.get("isgroup", False):
            username = cmsg.actual_user_nickname
            if username is None:
                username = cmsg.actual_user_id
        else:
            username = cmsg.from_user_nickname
            if username is None:
                username = cmsg.from_user_id

        is_triggered = False
        content = context.content
        if context.get("isgroup", False): # 群聊
            # 校验关键字
            match_prefix = check_prefix(content, conf().get('group_chat_prefix'))
            match_contain = check_contain(content, conf().get('group_chat_keyword'))
            if match_prefix is not None or match_contain is not None:
                is_triggered = True
            if context['msg'].is_at and not conf().get("group_at_off", False):
                is_triggered = True
        else: # 单聊
            match_prefix = check_prefix(content, conf().get('single_chat_prefix',['']))
            if match_prefix is not None:
                is_triggered = True

        self._insert_record(session_id, cmsg.msg_id, username, context.content, str(context.type), cmsg.create_time, int(is_triggered))
        # logger.debug("[Summary] {}:{} ({})" .format(username, context.content, session_id))

    def _check_tokens(self, records, max_tokens=3600):
        query = ""
        for record in records[::-1]:
            username = record[2]
            content = record[3]
            is_triggered = record[6]
            if record[4] in [str(ContextType.IMAGE),str(ContextType.VOICE)]:
                content = f"[{record[4]}]"
            
            sentence = ""
            if is_triggered:
                sentence += "T "
            sentence += f'{username}' + ": \"" + content + "\""
            query += "\n\n"+sentence
        prompt = "你是一位群聊机器人，需要对聊天记录进行简明扼要的摘要总结，用列表的形式输出，尽量包含说话人名字。\n聊天记录格式：[x]是emoji表情或者是对图片和声音文件的说明，某些消息前的T字母表示消息触发了群聊机器人的回复，内容大多是提问，若带有特殊符号如#和$一般是触发你无法感知的某个插件功能，聊天记录中不包含你对这类消息的回复，这类消息可以降低权重。请不要在回复中包含聊天记录格式中出现的符号。\n"
        
        firstmsg_id = records[0][1]
        session = self.bot.sessions.build_session(firstmsg_id, prompt)

        session.add_query("需要你总结的聊天记录如下：%s"%query)
        if  session.calc_tokens() > max_tokens:
            # logger.debug("[Summary] summary failed, tokens: %d" % session.calc_tokens())
            return None
        return session

    def _split_messages_to_summarys(self, records, max_tokens_persession=3600 , max_summarys=6):
        summarys = []
        count = 0
        while len(records) > 0 and len(summarys) < max_summarys:
            session = self._check_tokens(records,max_tokens_persession)
            last = 0
            if session is None:
                left,right = 0, len(records)
                while left < right:
                    mid = (left + right) // 2
                    logger.debug("[Summary] left: %d, right: %d, mid: %d" % (left, right, mid))
                    session = self._check_tokens(records[:mid], max_tokens_persession)
                    if session is None:
                        right = mid - 1
                    else:
                        left = mid + 1
                session = self._check_tokens(records[:left-1], max_tokens_persession)
                last = left
                logger.debug("[Summary] summary %d messages" % (left))
            else:
                last = len(records)
                logger.debug("[Summary] summary all %d messages" % (len(records)))
            if session is None:
                logger.debug("[Summary] summary failed, session is None")
                break
            logger.debug("[Summary] session query: %s, prompt_tokens: %d" % (session.messages, session.calc_tokens()))
            result = self.bot.reply_text(session)
            total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], result['content']
            logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (total_tokens, completion_tokens, reply_content))
            if completion_tokens == 0:
                if len(summarys) == 0:
                    return count,reply_content
                else:
                    break
            summary = reply_content
            summarys.append(summary)
            records = records[last:]
            count += last
        return count,summarys


    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type != ContextType.TEXT:
            return
        
        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        clist = content.split()
        if clist[0] == trigger_prefix+"总结":
            msg:ChatMessage = e_context['context']['msg']
            session_id = msg.from_user_id
            if conf().get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
                session_id = msg.from_user_nickname # itchat channel id会变动，只好用名字作为session id
            limit = 99
            if len(clist) > 1:
                limit = int(clist[1])
                logger.debug("[Summary] limit: %d" % limit)
            records = self._get_records(session_id, 0, limit)
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
            
            max_tokens_persession = 3600

            count, summarys = self._split_messages_to_summarys(records, max_tokens_persession)
            if count == 0 :
                if isinstance(summarys,str):
                    reply = Reply(ReplyType.ERROR, summarys)
                else:
                    reply = Reply(ReplyType.ERROR, "总结聊天记录失败")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return


            if len(summarys) == 1:
                reply = Reply(ReplyType.TEXT, f"本次总结了{count}条消息。\n\n"+summarys[0])
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            
            query = ""
            for i,summary in enumerate(reversed(summarys)):
                query += f"第{i}段摘要内容:\n"+summary + "\n----------------\n\n"
            prompt = "你是一位群聊机器人，聊天记录已经在你的大脑中被你总结成多段摘要总结，你需要对它们进行摘要总结，最后输出一篇完整的摘要总结，用列表的形式输出，在回复中务必不要体现原始输入是多段摘要总结。\n"
            
            session = self.bot.sessions.build_session(session_id, prompt)
            session.add_query("需要你总结的多段摘要内容如下：\n%s"%query)
            result = self.bot.reply_text(session)
            total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], result['content']
            logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (total_tokens, completion_tokens, reply_content))
            if completion_tokens == 0:
                reply = Reply(ReplyType.ERROR, "合并摘要失败，"+reply_content+"\n原始多段摘要如下：\n"+query)
            else:
                reply = Reply(ReplyType.TEXT, f"本次总结了{count}条消息(分段总结方式)。\n\n"+reply_content)     
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS # 事件结束，并跳过处理context的默认逻辑


    def get_help_text(self, verbose = False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入\"{trigger_prefix}总结 最近消息数量\"，我会帮助你总结聊天记录。\n例如：\"{trigger_prefix}总结 100\"，我会帮你总结最近100条消息。\n"
        return help_text
