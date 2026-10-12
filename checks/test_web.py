import json
from threading import Event
from unittest.mock import patch

import pytest
from gum_code.tools import web
from gum_code.tools.registry import execute_tool, get_tool_schemas, get_read_only_tool_schemas


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://224.0.0.1/', 'http://127.0.0.1', 'http://10.0.0.1', 'http://169.254.169.254/', 'http://localhost/', 'http://service.local/', 'https://user:pass@example.com', 'https://example.com:444/', 'https://example.com\\@127.0.0.1/', 'http://[::1]/', 'http://example.com\n/'])
def test_bad_urls(url):
    with pytest.raises(ValueError):
        web.validate_url(url)


def test_public_url():
    assert web.validate_url('https://docs.python.org/3/')[1] == 'docs.python.org'


def test_consent_and_desktop_gate(tmp_path):
    args = json.dumps({'query': 'python asyncio docs'})
    with patch.object(web, 'fetch') as fetch:
        assert 'desktop' in execute_tool(tmp_path, 'web_search', args)
        assert 'Approval required' in execute_tool(tmp_path, 'web_search', args, allow_web=True)
        assert 'denied' in execute_tool(tmp_path, 'web_search', args, allow_web=True, approval_callback=lambda *a: False)
        fetch.assert_not_called()
    default = {s['function']['name'] for s in get_tool_schemas() + get_read_only_tool_schemas()}
    assert not {'web_search','read_webpage'} & default
    assert {'web_search','read_webpage'} <= {s['function']['name'] for s in get_tool_schemas(allow_web=True)}


def test_invalid_before_consent(tmp_path):
    with patch.object(web, 'fetch') as fetch:
        approval = lambda *a: pytest.fail('invalid call requested consent')
        assert 'Error' in execute_tool(tmp_path,'web_search','{"query":null}',allow_web=True,approval_callback=approval)
        assert 'Error' in execute_tool(tmp_path,'read_webpage','{"url":"http://127.0.0.1"}',allow_web=True,approval_callback=approval)
        fetch.assert_not_called()


def test_parser_and_sources(tmp_path):
    rss = '<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F">Docs</a><td class="result-snippet">Official</td>'
    with patch.object(web, 'fetch', return_value=('https://lite.duckduckgo.com', 'text/html',rss)):
        result=execute_tool(tmp_path,'web_search','{"query":"python"}',allow_web=True,approval_callback=lambda *a:True)
        assert json.loads(result)['results'][0]['url']=='https://docs.python.org/3/'
    assert web.plain_html('<h1>Title</h1><script>evil()</script><p>Hello &amp; world</p>')=='Title\nHello & world'
    with pytest.raises(ValueError):web.search_results('<html>challenge</html>')


def test_cancelled(tmp_path):
    event=Event();event.set()
    with patch.object(web,'_public_address') as dns:
        assert '已停止' in web.web_search(workspace=tmp_path,query='python',cancel_event=event)
        dns.assert_not_called()


def test_read_complete_text_and_safe_error(tmp_path):
    with patch.object(web,'fetch',return_value=('https://example.com/final','text/plain','x'*30000+'FINAL_SENTINEL')):
        data=json.loads(web.read_webpage(workspace=tmp_path,url='https://example.com'))
        assert not data['truncated'] and data['text'].endswith('FINAL_SENTINEL')
        assert data['total_chars']==len(data['text'])==30014 and data['scope']=='full_static_text'
        assert data['url'].endswith('/final') and data['next_start_index'] is None
    with patch.object(web,'fetch',side_effect=OSError('private diagnostic')):
        assert 'private diagnostic' not in web.read_webpage(workspace=tmp_path,url='https://example.com')


def test_private_dns_answer():
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return b'{"Answer":[{"type":1,"data":"127.0.0.1"}]}'
    with patch.object(web,'build_opener') as opener:
        opener.return_value.open.return_value=Response()
        with pytest.raises(ValueError):web._public_address('example.com',None,web.time.monotonic()+20)


def test_redirect_is_revalidated():
    class Response:
        status=302
        def getheader(self,key,*a):return 'http://127.0.0.1/'
    with patch.object(web,'_public_address',return_value='93.184.215.14'),patch.object(web.socket,'create_connection'),patch.object(web.http.client,'HTTPConnection') as conn:
        conn.return_value.timeout=5
        conn.return_value.getresponse.return_value=Response()
        with pytest.raises(ValueError):web.fetch('http://example.com/')
        assert conn.call_count==1


def test_size_cap():
    class Response:
        status=200
        def getheader(self,key,default=None):return 'text/plain' if key=='Content-Type' else default
        def read1(self,size):return b'x'*size
    with patch.object(web,'_public_address',return_value='93.184.215.14'),patch.object(web.socket,'create_connection'),patch.object(web.http.client,'HTTPConnection') as conn:
        conn.return_value.timeout=5
        conn.return_value.getresponse.return_value=Response()
        with pytest.raises(ValueError,match='1 MiB'):web.fetch('http://example.com/')
        conn.return_value.close.assert_called()


@pytest.mark.parametrize('name',['web_search','read_webpage'])
def test_interrupted_read_closes_without_replay(name):
    from gum_code.session import IN_FLIGHT_MARKER_KEY, build_tool_batch_recovery_messages
    messages=[{'role':'user','content':'public web check'},
              {'role':'assistant','content':None,'tool_calls':[{'id':'web','type':'function','function':{'name':name,'arguments':'{}'}}]},
              {'role':'tool','tool_call_id':'web','content':'',IN_FLIGHT_MARKER_KEY:True}]
    with patch.object(web,'fetch') as fetch:
        restored=build_tool_batch_recovery_messages(messages)
        assert not any(m.get(IN_FLIGHT_MARKER_KEY) for m in restored)
        assert restored[-1]['role']=='tool'
        assert '被中断' in restored[-1]['content'] and '未保留可用结果' in restored[-1]['content']
        fetch.assert_not_called()


def test_invalid_result_cannot_attach_snippet_to_previous_source():
    results=web.search_results('<a class="result-link" href="https://example.com">Good</a><td class="result-snippet">Good snippet</td><a class="result-link" href="http://127.0.0.1/">Bad</a><td class="result-snippet">Wrong snippet</td>')
    assert len(results)==1 and results[0]['snippet']=='Good snippet'


def test_deadline():
    with pytest.raises(ValueError,match='超时'):
        web._check(None,web.time.monotonic()-1)


def test_legacy_offset_still_returns_complete_snapshot(tmp_path):
    body='a'*24000+'remaining'
    with patch.object(web, 'fetch', return_value=('https://example.com', 'text/plain', body)):
        result=execute_tool(tmp_path,'read_webpage',json.dumps({'url':'https://example.com','start_index':24000}),allow_web=True,approval_callback=lambda *a:True)
        data=json.loads(result)
        assert data['text']==body and data['start_index']==0
        assert not data['truncated'] and data['next_start_index'] is None
    for invalid in [-1,True,1.5]:
        with pytest.raises(ValueError):web.validate_web(workspace=tmp_path,url='https://example.com',start_index=invalid)


@pytest.mark.parametrize('body', ['x'*120001, ' ', '\x01'*30000])
def test_no_successful_partial_or_empty_response(tmp_path,body):
    with patch.object(web,'fetch',return_value=('https://example.com','text/plain',body)):
        result=web.read_webpage(workspace=tmp_path,url='https://example.com')
        assert result.startswith('Error:') and '不能声称已读全文' in result


def test_full_text_at_limit(tmp_path):
    body='x'*119999+'Z'
    with patch.object(web,'fetch',return_value=('https://example.com','text/plain',body)):
        result=json.loads(web.read_webpage(workspace=tmp_path,url='https://example.com'))
        assert result['text']==body and not result['truncated']


def test_short_http_body_cannot_be_complete():
    class Response:
        status=200
        def getheader(self,key,default=None):
            return {'Content-Type':'text/plain','Content-Length':'100'}.get(key,default)
        blocks=iter([b'partial',b''])
        def read1(self,size):return next(self.blocks)
    with patch.object(web,'_public_address',return_value='93.184.215.14'),patch.object(web.socket,'create_connection'),patch.object(web.http.client,'HTTPConnection') as conn:
        conn.return_value.timeout=5
        conn.return_value.getresponse.return_value=Response()
        with pytest.raises(ValueError,match='正文不完整'):web.fetch('http://example.com/')


def test_timeout_reports_no_complete_body(tmp_path):
    with patch.object(web,'fetch',side_effect=TimeoutError):
        result=web.read_webpage(workspace=tmp_path,url='https://example.com')
        assert result.startswith('Error:') and '超时' in result and '没有取得完整正文' in result
