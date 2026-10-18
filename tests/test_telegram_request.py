import json
from types import SimpleNamespace
from urllib.parse import urlencode

from khtb_lab.database import Database
from khtb_lab.telegram_bot import TelegramBot

def test_allowed_updates_is_one_json_parameter():
    value=json.dumps(["message","callback_query"])
    encoded=urlencode({"allowed_updates":value})
    assert encoded.count("allowed_updates=")==1
    assert "message" in encoded and "callback_query" in encoded

def test_expected_ui_callbacks():
    callbacks={"status","scan","auto","manual","safe","adventurer","coins","trades","closeall"}
    assert {"auto","manual","status","scan"}.issubset(callbacks)

def test_each_open_trade_has_its_own_status_message_and_close_button():
    db=Database(':memory:')
    first=db.open_trade('BTC/USDT','LONG',100,95,110,1)
    second=db.open_trade('ETH/USDT','SHORT',200,210,180,2)
    app=SimpleNamespace(db=db)
    bot=TelegramBot.__new__(TelegramBot)
    bot.settings=SimpleNamespace(allowed_users=[123])
    bot.app=app
    requests=[]

    def request(method,payload,timeout=15):
        requests.append((method,payload))
        return {'message_id':len(requests)}

    bot._request=request
    bot._publish_trade_status(
        {'id':first,'symbol':'BTC/USDT','direction':'LONG','entry':100,
         'stop':95,'target':110,'qty':1},
        102,2,
    )
    bot._publish_trade_status(
        {'id':second,'symbol':'ETH/USDT','direction':'SHORT','entry':200,
         'stop':210,'target':180,'qty':2},
        198,4,
    )

    assert [call[0] for call in requests]==['sendMessage','sendMessage']
    assert requests[0][1]['reply_markup']['inline_keyboard'][0][0][
        'callback_data'
    ]==f'close_trade:{first}'
    assert requests[1][1]['reply_markup']['inline_keyboard'][0][0][
        'callback_data'
    ]==f'close_trade:{second}'
    assert db.trade_followup(first,123)['message_id']==1
    assert db.trade_followup(second,123)['message_id']==2
    db.close()

def test_trade_milestones_use_one_r_and_ninety_percent_target_progress():
    from khtb_lab.app import KhtbLab

    trade={'id':1,'symbol':'BTC/USDT','direction':'LONG',
           'entry':100,'stop':95,'target':110,'qty':1}
    events=KhtbLab.__new__(KhtbLab).trade_milestones(trade,109)

    assert [event for event,_ in events]==['near_target','tp1_reached']

def test_closed_trade_status_removes_individual_close_button():
    db=Database(':memory:')
    trade_id=db.open_trade('BTC/USDT','LONG',100,95,110,1)
    db.save_trade_followup(trade_id,123,8001,'old-hash')
    bot=TelegramBot.__new__(TelegramBot)
    bot.settings=SimpleNamespace(allowed_users=[123])
    bot.app=SimpleNamespace(db=db)
    calls=[]
    bot._request=lambda method,payload,timeout=15: calls.append(
        (method,payload)
    ) or {}

    bot.publish_trade_status(
        {'id':trade_id,'symbol':'BTC/USDT','direction':'LONG','entry':100,
         'stop':95,'target':110,'qty':1,'status':'CLOSED'},
        108,8,closed=True,reason='TAKE_PROFIT',
    )

    assert calls[0][0]=='editMessageText'
    assert calls[0][1]['reply_markup']=={'inline_keyboard':[]}
    db.close()

def test_close_callback_targets_only_the_requested_trade():
    called=[]
    app=SimpleNamespace(
        close_trade_from_telegram=lambda trade_id: called.append(trade_id)
        or {'success':False,'error':'test'},
    )
    bot=TelegramBot.__new__(TelegramBot)
    bot.settings=SimpleNamespace(allowed_users=[123])
    bot.app=app
    sent=[]
    bot.send=lambda chat_id,text,keyboard=None: sent.append(
        (chat_id,text)
    ) or True
    bot._answer_callback=lambda *args,**kwargs: None

    bot._dispatch_callback({
        'id':'callback-1',
        'from':{'id':123},
        'message':{'chat':{'id':123}},
        'data':'close_trade:42',
    })

    assert called==[42]
    assert sent and 'لم يتأكد إغلاق الصفقة' in sent[0][1]
