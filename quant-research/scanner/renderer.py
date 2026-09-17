import pandas as pd
import numpy as np
from html import escape
from scanner.data_adapter import fetch_live_quote

def entry_context(current: float, close: float) -> tuple[str, float, float]:
    """Descriptive experiment bands, not validated buy-price limits."""
    if pd.isna(current) or pd.isna(close) or close<=0 or current<=0:
        return '가격 비교 불가',np.nan,np.nan
    gap=(current/close-1)*100
    band=('종가 이하' if gap<=0 else '종가 +0~3%' if gap<=3+1e-9 else
          '종가 +3~5%' if gap<=5+1e-9 else '종가 +5% 초과')
    return band,close*1.03,close*1.05

def mobile_table_html(table: pd.DataFrame) -> str:
    def price(x):
        return f'{x:,.0f}원' if pd.notna(x) else '—'
    def change(x):
        if pd.isna(x):return '<span class="muted">—</span>'
        color='up' if x>0 else ('down' if x<0 else 'muted')
        return f'<b class="{color}">{x:+.2f}%</b>'
    body=[]
    watch='단계' in table.columns
    for r in table.to_dict('records'):
        current=r.get('현재가',np.nan);level=r['돌파선'];close=r.get('돌파봉종가',np.nan)
        name=escape(str(r['종목']));code=escape(str(r['코드']))
        state='다음 봉 마감 대기' if watch else '패턴 충족'
        tag='wait' if watch else 'ready'
        amount=r['대금(추정·억원)'] if watch else r['돌파봉대금(추정·억원)']
        ratio=r['동시간배수'] if watch else r['돌파봉대금배수']
        gap=lambda ref:(current/ref-1)*100 if pd.notna(current) and pd.notna(ref) and ref>0 else np.nan
        stamp=str(r.get('시세시각','시세 없음'))
        try:stamp=pd.Timestamp(stamp).tz_convert('Asia/Seoul').strftime('%m-%d %H:%M:%S')
        except (ValueError,TypeError):pass
        note=escape(str(r.get('시세오류','')))
        high=r.get('당일고가',np.nan)
        retreat=f'{current-high:+,.0f}원' if pd.notna(current) and pd.notna(high) else '—'
        band,reference3,reference5=entry_context(current,close)
        body.append(f'<tr class="heading"><td colspan="2"><b>{name}</b> <small>{code}</small>'
                    f'<span class="badge {tag}">{state}</span></td></tr>'
                    f'<tr><td>네이버 현재가</td><td><strong>{price(current)}</strong> {note}</td></tr>'
                    f'<tr><td>돌파선</td><td>{price(level)} <span class="muted">대비</span> {change(gap(level))}</td></tr>'
                    f'<tr><td>돌파봉 종가</td><td>{price(close)} <span class="muted">대비</span> {change(gap(close))}</td></tr>'
                    f'<tr><td>진입 이격 구간</td><td>{escape(band)}<br><small>종가 +3% {price(reference3)}<br>종가 +5% {price(reference5)}<br>비교 가격이며 추천 매수가 아님</small></td></tr>'
                    f'<tr><td>당일 등락</td><td>전일 대비 {change(r.get("당일등락률",np.nan))}</td></tr>'
                    f'<tr><td>당일 고가</td><td><b>{price(high)}</b><br>현재가 차이 {retreat}<br>고가 대비 {change(gap(high))}</td></tr>'
                    f'<tr><td>거래대금</td><td>당일 {escape(str(r.get("당일누적대금","미제공")))}<br><small>돌파봉 추정 {amount:,.1f}억 · {ratio:,.2f}배</small></td></tr>'
                    f'<tr class="time"><td colspan="2">시세 {escape(stamp)} KST · {escape(str(r.get("시세상태","미확인")))}<br>'
                    f'돌파 {escape(str(r["돌파봉(KST)"]))} · 데이터 {escape(str(r["기준봉(KST)"]))}<br>'
                    f'{escape(str(r.get("시세출처","네이버 시세 없음")))}</td></tr>')
    return ('<style>.scan-mobile{width:100%;max-width:480px;border-collapse:collapse;table-layout:fixed;font:14px/1.55 sans-serif;color:#edf2fa;background:#111827;color-scheme:dark;}'
            '.scan-mobile td{padding:6px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere;white-space:normal;border-bottom:1px solid #2b374b;}'
            '.scan-mobile .heading{background:#233047;}.scan-mobile strong{font-size:21px;}.scan-mobile small,.scan-mobile .time{font-size:12px;}'
            '.scan-mobile .muted,.scan-mobile small{color:#b4c2d6;}.scan-mobile .up{color:#ff929c;}.scan-mobile .down{color:#87baff;}'
            '.scan-mobile .badge{float:right;padding:2px 7px;border-radius:5px;font-size:12px;font-weight:bold;}'
            '.scan-mobile .wait{background:#574216;color:#ffe09a;}.scan-mobile .ready{background:#164c3b;color:#a0efd1;}'
            '.scan-mobile .time td{padding-bottom:14px;border-bottom:3px solid #465570;color:#b4c2d6;}</style>'
            '<table class="scan-mobile"><colgroup><col style="width:34%"><col style="width:66%"></colgroup><tbody>'+''.join(body)+'</tbody></table>')

def show_table(table: pd.DataFrame) -> None:
    try:
        from IPython import get_ipython
        from IPython.display import display, HTML
        if get_ipython() is None:
            print(table.to_string(index=False))
        else:
            display(HTML(mobile_table_html(table)))
    except ImportError:
        print(table.to_string(index=False))

def show_diagnostics(lines: list[str]) -> None:
    try:
        from IPython import get_ipython
        from IPython.display import display, HTML
        if get_ipython() is not None:
            display(HTML('<details><summary>조회 상세 펼치기</summary><div style="font-size:12px;overflow-wrap:anywhere">'+
                         '<br>'.join(escape(line) for line in lines)+'</div></details>'))
            return
    except ImportError:
        pass
    print('\n'.join(lines))

def attach_quotes(tables: list[pd.DataFrame]) -> list[pd.DataFrame]:
    codes=sorted({str(code) for t in tables if not t.empty for code in t['코드']})
    quotes={code:fetch_live_quote(code) for code in codes}
    output=[]
    for table in tables:
        t=table.copy()
        if not t.empty:
            extra=pd.DataFrame([quotes[str(code)] for code in t['코드']],index=t.index)
            for col in extra:t[col]=extra[col]
        output.append(t)
    return output

def tag_repeats(result: pd.DataFrame, day: str, seen: set) -> pd.DataFrame:
    result = result.copy()
    labels=[]
    for code in result['코드']:
        item=(day,code)
        labels.append('재등장' if item in seen else '신규')
        seen.add(item)
    result.insert(3,'등장',labels)
    return result

def diagnostics(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    count=lambda key:sum(bool(r[key]) for r in rows)
    times=pd.Series([pd.Timestamp(r['_bar_time']) for r in rows])
    distribution=times.value_counts().sort_index()
    stamps=' / '.join(f'{t:%m-%d %H:%M} {n}종목' for t,n in distribution.items())
    return [f"조건 통과: 기본패턴 {count('_base')} → 직전봉 돌파 {count('_breakout')} → 10억원 {count('_liquid')} → 동시간 2배 {count('_trigger')} → 유지 확인 {count('_match')}",
            f"기본패턴·현재봉 돌파·대금 충족 {count('_waiting')}종목: 다음 봉 유지 확인 전이며 매수 후보가 아닙니다.",
            '기준봉 분포(KST·봉 시작): '+stamps,
            '종목별 마지막 완료봉 기준입니다. 과거 시각이면 지연 또는 거래일 여부를 확인하세요.']
