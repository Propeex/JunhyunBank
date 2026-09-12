from junhyunbank.storage import Storage


def test_activity_counts_only_priced_live_fills_and_keeps_pending_separate(tmp_path):
    s=Storage(tmp_path/'activity.db')
    for mode,quantity in [('LIVE',1),('LIVE',0),('PAPER',1)]:
        s.trade(mode=mode,market='KRW-X',side='BUY',quantity=quantity,amount_krw=100,price=100,reason='fixture')
    s.create_order_intent('pending','KRW-X','SELL',{})
    first=s.activity_snapshot(); second=s.activity_snapshot()
    assert first==second
    assert first['counts']=={'BUY':1}
    assert len(first['trades'])==1
    assert len(first['pending'])==1 and first['pending'][0]['side']=='SELL'


def test_zero_fill_completion_is_not_a_trade(tmp_path):
    s=Storage(tmp_path/'activity.db')
    s.create_order_intent('zero','KRW-X','BUY',{})
    assert s.complete_order_intent('zero',dict(uuid='zero',state='cancel',executed_volume='0'))
    assert s.activity_snapshot()['trades']==[]
    assert s.activity_snapshot()['counts']=={}
    assert s.activity_snapshot()['pending']==[]
