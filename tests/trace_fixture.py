"""Synthetic archive for trace navigation, pagination, and history replacement."""
import argparse
from pathlib import Path
from replica.store import Store, encode
from replica.view import View, session_key
from replica.normalize import text_content

ORIGIN = 'fixture:/home-a'
SID = session_key(ORIGIN, 'same-id')

def populate(root, revert=False):
    Store(root).close()
    view = View(root)
    with view.db:
        if revert:
            for row in view.db.execute("SELECT key FROM entities WHERE session=? AND kind='item'", (SID,)).fetchall():
                view.delete_entity(row['key'])
        else:
            for origin, title in [(ORIGIN, '长历史样本'), ('fixture:/home-b', '同 ID 另一来源')]:
                sid = session_key(origin, 'same-id')
                view.set_entity('thread-'+sid, 'thread', sid, {'id':sid, 'thread_id':'same-id', 'origin':origin, 'title':title, 'updated_at':1790000000, 'archived':False, 'items':724 if origin==ORIGIN else 0, 'coverage':{'status':'partial', 'issues':['unknown_record']}})
            for n in range(600):
                turn = 'turn-'+str(n)
                item = {'type':'agentMessage','id':'answer-'+str(n),'text':'TURN '+str(n), 'phase':'final_answer'}
                view.set_entity(encode(['item',SID,turn,item['id']]).decode(), 'item', SID, {'session':SID,'turn':turn,'position':n+124,'item':item,'text':item['text'],'source':{'generation':'current-head','start':str(n*100),'end':str(n*100+100)}})
            for n in range(124):
                if n == 1:
                    item = {'type':'unknownFixtureItem','id':'p-'+str(n),'payload':'UNKNOWN VISIBLE'}
                elif n == 2:
                    item = {'type':'reasoning','id':'p-'+str(n),'summary':['VISIBLE REASONING'],'content':[]}
                elif n == 3:
                    item = {'type':'commandExecution','id':'p-'+str(n),'command':'echo fixture','aggregatedOutput':'FIXTURE OUTPUT','status':'completed'}
                else:
                    item = {'type':'agentMessage','id':'p-'+str(n),'phase':'final_answer','text':'PAGE '+str(n)}
                view.set_entity(encode(['item',SID,'long-turn',item['id']]).decode(), 'item', SID, {'session':SID,'turn':'long-turn','position':n,'item':item,'text':text_content(item),'source':{'generation':'parent-prefix','start':str(n*100),'end':str(n*100+100)}})
        if revert:
            item={'type':'agentMessage','id':'selected-head-item','text':'REVERTED HEAD','phase':'final_answer'}
            view.set_entity('new-head-item','item',SID,{'session':SID,'turn':'new-head','position':0,'item':item,'text':item['text'],'source':{'generation':'reverted-head','start':'0','end':'100'}})
    view.close()
    if not revert:
        from replica.live import LiveJournal
        journal=LiveJournal(root)
        journal.append(ORIGIN,'fixture-epoch','event',{'method':'item/completed','params':{'threadId':'same-id','turnId':'long-turn','item':{'type':'agentMessage','id':'p-0','text':'STALE NOTIFICATION'}}})
        journal.close()

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--revert',action='store_true')
    args=parser.parse_args()
    args.root.mkdir(parents=True,exist_ok=True)
    populate(args.root,args.revert)
