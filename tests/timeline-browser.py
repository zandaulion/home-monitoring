"""Standalone browser regression with synthetic data only.
Run: .venv/bin/python tests/timeline-browser.py
Set PLAYWRIGHT_CHROMIUM_EXECUTABLE when using a separately installed Chromium.
"""
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import tempfile, threading, time, socket
from pathlib import Path
import cv2, numpy as np
import uvicorn
from playwright.sync_api import sync_playwright, expect
from home_monitor.app import create_app

class Vision:
    pass

def run():
    with tempfile.TemporaryDirectory(prefix='timeline-ui-') as folder:
        config={'data_dir':folder,'model_dir':folder,'cameras':[{'id':'yard','name':'Synthetic yard'},{'id':'gate','name':'Synthetic gate'}],'owner_login':'owner@example.invalid','collector_token':'synthetic','admin_token':'synthetic-diagnostic','allowed_origins':['http://127.0.0.1:8767']}
        app=create_app(config,Vision(),push_sender=lambda *_:201)
        store=app.state.store
        date=__import__('datetime').datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        _,image=cv2.imencode('.jpg',np.full((160,240,3),140,dtype=np.uint8))
        for i in range(130):
            state=['known','unknown','unavailable'][i%3]
            now=date+14*3600+i*25
            store.record_event('yard',state,1,[],image.tobytes(),[],now=now)
        for i in range(8):
            store.record_event('gate','unknown',1,[],image.tobytes(),[],now=date+3600*(i+2))
        store.record_coverage('yard','online',time.time()-300)
        store.record_coverage('yard','error',time.time()-250)
        store.record_coverage('gate','online',time.time()-50)
        store.execute('INSERT INTO alert_snoozes(start,end) VALUES(?,?)',(date+14*3600,date+15*3600))
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8767,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),args=['--no-sandbox'])
            context=browser.new_context(extra_http_headers={'Tailscale-User-Login':'owner@example.invalid'},viewport={'width':1280,'height':1000})
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(error.stack))
            page.goto('http://127.0.0.1:8767/#events')
            page.wait_for_selector('.timeline-period')
            assert '138 events' in page.locator('#timeline-status').inner_text()
            assert page.locator('.timeline-lane').count()==2
            axis=page.locator('.timeline-axis > div').last.bounding_box()
            assert axis['width'] > 300
            page.get_by_role('button', name='Synthetic yard', exact=False).first.click()
            page.wait_for_selector('#all-events .event-row')
            assert page.locator('#all-events .event-row').count()==12
            page.locator('#timeline-more').click()
            expect(page.locator('#all-events .event-row')).to_have_count(24)
            page.wait_for_timeout(5200)
            expect(page.locator('#all-events .event-row')).to_have_count(24)
            page.locator('#timeline-zoom').select_option('7200')
            page.locator('#timeline-focus').fill('14.5');page.locator('#timeline-focus').dispatch_event('input')
            page.wait_for_timeout(300)
            page.locator('#all-events .event-row').first.click()
            page.wait_for_selector('#modal[open]')
            page.locator('#done-event').click()
            (ROOT / '.runtime/qa').mkdir(parents=True, exist_ok=True)
            page.wait_for_timeout(4500)
            page.screenshot(path=str(ROOT / '.runtime/qa/timeline-desktop.png'),full_page=True)
            page.locator('#state-filter').select_option('unknown')
            expect(page.locator('#timeline-status')).to_contain_text('51 events')
            page.locator('#state-filter').select_option('')
            expect(page.locator('#timeline-status')).to_contain_text('138 events')
            page.set_viewport_size({'width':375,'height':812})
            page.wait_for_timeout(300)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile page overflows'
            page.screenshot(path=str(ROOT / '.runtime/qa/timeline-mobile.png'),full_page=True)
            page.locator('#timeline-day').fill('2000-01-01');page.locator('#timeline-day').dispatch_event('change')
            expect(page.locator('#timeline-status')).to_contain_text('No activity')
            assert page.locator('.timeline-period').count()==0
            assert not errors, errors
            print('Browser passed: whole-day counts, camera lanes, selection, pagination, zoom, focus, event modal, filters, mobile width, empty day; no JS errors')
            touch=browser.new_context(extra_http_headers={'Tailscale-User-Login':'owner@example.invalid'},viewport={'width':320,'height':812},is_mobile=True,has_touch=True)
            phone=touch.new_page();phone.on('pageerror',lambda error:errors.append(error.stack))
            phone.goto('http://127.0.0.1:8767/#events');phone.wait_for_selector('.timeline-period')
            assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth')
            for track in phone.locator('.timeline-track').all():
                boxes=[button.bounding_box() for button in track.locator('[data-period]').all()]
                for i, box in enumerate(boxes):
                    assert box['width'] >= 44
                    for other in boxes[i+1:]:
                        assert box['y'] != other['y'] or box['x']+box['width'] <= other['x'] or other['x']+other['width'] <= box['x']
            assert not errors,errors
            expect(phone.locator('#alert-mode')).to_have_text('Alerts active')
            assert phone.locator('#alert-controls').bounding_box()['height'] <= 84
            phone.locator('#snooze-alerts').click()
            expect(phone.get_by_role('dialog')).to_be_visible()
            phone.locator('#cancel-snooze').click()
            expect(phone.get_by_role('dialog')).not_to_be_visible()
            expect(phone.locator('#alert-mode')).to_have_text('Alerts active')
            phone.locator('#snooze-alerts').click()
            phone.get_by_role('button',name='1 hour',exact=True).click()
            expect(phone.get_by_role('dialog')).not_to_be_visible()
            expect(phone.locator('#alert-mode')).to_have_text('Alerts snoozed')
            expect(phone.locator('#alert-detail')).to_contain_text('all devices')
            assert phone.locator('#alert-controls').bounding_box()['height'] <= 84
            (ROOT / '.runtime/qa').mkdir(parents=True,exist_ok=True)
            phone.screenshot(path=str(ROOT / '.runtime/qa/compact-alerts-mobile.png'),full_page=True)
            phone.locator('#resume-alerts').click()
            expect(phone.locator('#alert-mode')).to_have_text('Alerts active')
            expect(phone.locator('#snooze-alerts')).to_be_visible()
            touch.close()
            # A missing module must leave a visible recovery screen, not a blank PWA.
            recovery=browser.new_context(service_workers='block')
            broken=recovery.new_page()
            broken.route('**/assets/timeline.js',lambda route:route.fulfill(status=401,content_type='application/json',body='{}'))
            broken.goto('http://127.0.0.1:8767/')
            expect(broken.locator('#startup')).to_be_visible()
            expect(broken.get_by_role('link',name='Refresh app files')).to_have_attribute('href','/bust')
            recovery.close()
            browser.close()
        server.should_exit=True;thread.join(5)

if __name__=='__main__':run()
