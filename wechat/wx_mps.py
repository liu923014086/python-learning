import json
import re
import time
from datetime import datetime

import requests

from utils import pgs


class WxMps(object):
    """微信公众号文章、评论抓取爬虫"""

    def __init__(self, _mps_id, _biz, _pass_ticket, _app_msg_token, _cookie, _offset=0):
        self.offset = _offset
        self.mps_id = _mps_id
        self.biz = _biz  # 公众号标志
        self.msg_token = _app_msg_token  # 票据(非固定)
        self.pass_ticket = _pass_ticket  # 票据(非固定)
        self.headers = {
            'Cookie': _cookie,  # Cookie(非固定)
            'User-Agent': 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/57.0.2987.132 '
        }
        wx_mps = 'wxmps'  # 这里数据库、用户、密码一致(需替换成实际的)
        self.postgres = pgs.Pgs(host='localhost', port='12432', db_name=wx_mps, user=wx_mps, password=wx_mps)

    def start(self):
        """请求获取公众号的文章接口"""

        offset = self.offset
        while True:
            api = 'https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz={0}&f=json&offset={1}' \
                  '&count=10&is_ok=1&scene=124&uin=777&key=777&pass_ticket={2}&wxtoken=&appmsg_token' \
                  '={3}&x5=1&f=json'.format(self.biz, offset, self.pass_ticket, self.msg_token)

            resp = requests.get(api, headers=self.headers, verify=False).json()
            ret, status = resp.get('ret'), resp.get('errmsg')  # 状态信息
            if ret == 0 or status == 'ok':
                print('Crawl article: ' + api)
                offset = resp['next_offset']  # 下一次请求偏移量
                general_msg_list = resp['general_msg_list']
                msg_list = json.loads(general_msg_list)['list']  # 获取文章列表
                for msg in msg_list:
                    comm_msg_info = msg['comm_msg_info']  # 该数据是本次推送多篇文章公共的
                    msg_id = comm_msg_info['id']  # 文章id
                    post_time = datetime.fromtimestamp(comm_msg_info['datetime'])  # 发布时间
                    # msg_type = comm_msg_info['type']  # 文章类型
                    # msg_data = json.dumps(comm_msg_info, ensure_ascii=False)  # msg原数据

                    app_msg_ext_info = msg.get('app_msg_ext_info')  # article原数据
                    if app_msg_ext_info:
                        # 本次推送的首条文章
                        self._parse_articles(app_msg_ext_info, msg_id, post_time)
                        # 本次推送的其余文章
                        multi_app_msg_item_list = app_msg_ext_info.get('multi_app_msg_item_list')
                        if multi_app_msg_item_list:
                            for item in multi_app_msg_item_list:
                                msg_id = item['fileid']  # 文章id
                                if msg_id == 0:
                                    msg_id = int(time.time() * 1000)  # 设置唯一id,解决部分文章id=0出现唯一索引冲突的情况
                                self._parse_articles(item, msg_id, post_time)
                print('next offset is %d' % offset)
            else:
                print('Before break , Current offset is %d' % offset)
                break

    def _parse_articles(self, info, msg_id, post_time):
        """解析嵌套文章数据并保存入库"""

        title = info.get('title')  # 标题
        cover = info.get('cover')  # 封面图
        author = info.get('author')  # 作者
        digest = info.get('digest')  # 关键字
        source_url = info.get('source_url')  # 原文地址
        content_url = info.get('content_url')  # 微信地址
        # ext_data = json.dumps(info, ensure_ascii=False)  # 原始数据

        content_url = content_url.replace('amp;', '').replace('#wechat_redirect', '').replace('http', 'https')
        article_id = self.postgres.handler(self._save_article(), (msg_id, title, author, cover, digest,
                                                                  source_url, content_url, post_time,
                                                                  datetime.now(), self.mps_id), fetch=True)
        if article_id:
            self._parse_article_detail(content_url, article_id)

    def _parse_article_detail(self, content_url, article_id):
        """从文章页提取相关参数用于获取评论,article_id是已保存的文章id"""

        try:
            resp = requests.get(content_url, headers=self.headers, verify=False)
        except Exception as e:
            print('获取评论失败' + content_url)
            print(e)
        else:
            # group(0) is current line
            html = resp.text
            str_comment = re.search(r'var comment_id = "(.*)" \|\| "(.*)" \* 1;', html)
            str_msg = re.search(r"var appmsgid = '' \|\| '(.*)'\|\|", html)
            str_token = re.search(r'window.appmsg_token = "(.*)";', html)

            if str_comment and str_msg and str_token:
                comment_id = str_comment.group(1)  # 评论id(固定)
                app_msg_id = str_msg.group(1)  # 票据id(非固定)
                appmsg_token = str_token.group(1)  # 票据token(非固定)

                # 缺一不可
                if appmsg_token and app_msg_id and comment_id:
                    print('Crawl article comments: ' + content_url)
                    self._crawl_comments(app_msg_id, comment_id, appmsg_token, article_id)

    def _crawl_comments(self, app_msg_id, comment_id, appmsg_token, article_id):
        """抓取文章的评论"""

        api = 'https://mp.weixin.qq.com/mp/appmsg_comment?action=getcomment&scene=0&__biz={0}' \
              '&appmsgid={1}&idx=1&comment_id={2}&offset=0&limit=100&uin=777&key=777' \
              '&pass_ticket={3}&wxtoken=777&devicetype=android-26&clientversion=26060739' \
              '&appmsg_token={4}&x5=1&f=json'.format(self.biz, app_msg_id, comment_id,
                                                     self.pass_ticket, appmsg_token)
        resp = requests.get(api, headers=self.headers, verify=False).json()
        ret, status = resp['base_resp']['ret'], resp['base_resp']['errmsg']
        if ret == 0 or status == 'ok':
            elected_comment = resp['elected_comment']
            for comment in elected_comment:
                nick_name = comment.get('nick_name')  # 昵称
                logo_url = comment.get('logo_url')  # 头像
                comment_time = datetime.fromtimestamp(comment.get('create_time'))  # 评论时间
                content = comment.get('content')  # 评论内容
                content_id = comment.get('content_id')  # id
                like_num = comment.get('like_num')  # 点赞数

                reply_list = comment.get('reply')['reply_list']  # 回复数据
                reply_content, reply_like_num, reply_create_time = None, None, None
                if reply_list:
                    first_reply = reply_list[0]
                    reply_content = first_reply.get('content')
                    reply_like_num = first_reply.get('reply_like_num')
                    reply_create_time = datetime.fromtimestamp(first_reply.get('create_time'))

                self.postgres.handler(self._save_article_comment(), (article_id, comment_id, nick_name, logo_url,
                                                                     content_id, content, like_num, comment_time,
                                                                     datetime.now(), reply_content, reply_like_num,
                                                                     reply_create_time))
            time.sleep(3)

    @staticmethod
    def _save_article():
        sql = 'insert into tb_article(msg_id,title,author,cover,digest,source_url,content_url,post_time,create_time,' \
              'mps_id) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id'
        return sql

    @staticmethod
    def _save_article_comment():
        sql = 'insert into tb_article_comment(article_id,comment_id,nick_name,logo_url,content_id,content,like_num,' \
              'comment_time,create_time,reply_content,reply_like_num,reply_create_time) ' \
              'values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
        return sql


if __name__ == '__main__':
    _id = 4
    biz = 'MzU0MDExOTM3Mg=='
    pass_ticket = 'vvEyGVtFd3AmHq/FojRaNIpnRGppQWsKc90kUbOaYWfjkqZolATSNQZ3iSjhhgpc'
    app_msg_token = '981_YfDTVXEmxlQ50ppDO4B7KL3xHSnldDKS0-OaXg~~'
    cookie = 'rewardsn=; wxtokenkey=777; wxuin=1604513290; devicetype=Windows10; version=62060426; lang=zh_TW; pass_ticket=vvEyGVtFd3AmHq/FojRaNIpnRGppQWsKc90kUbOaYWfjkqZolATSNQZ3iSjhhgpc; wap_sid2=CIrci/0FElxqNDZBTGJUSTZ3YldLVUFmWFdETVF0VF9FckZPQ2pnOF8teXVzTjYtYjRxYnhpQjlGTnRRbXZEU043MkdjZ09iOEpVVUNGcF96dFpoMTh6ZnBuNExhdFVEQUFBfjDTkITfBTgNQJVO'
    # 以上信息不同公众号每次抓取都需要借助抓包工具做修改
    wxMps = WxMps(_id, biz, pass_ticket, app_msg_token, cookie)
    wxMps.start()  # 开始爬取文章及评论
