import pandas as pd

def select_top(rows: list[dict], top_n: int = 5) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    candidates = pd.DataFrame(rows)
    result = (candidates[candidates['_match']].sort_values(['점수','코드'],ascending=[False,True])
              .head(top_n).drop(columns=[c for c in candidates if c.startswith('_')]).reset_index(drop=True))
    result['점수'] = result['점수'].round(1)
    result.insert(0,'순위',range(1,len(result)+1))
    return result

def build_watch(rows: list[dict]) -> pd.DataFrame:
    observations=[]
    for r in rows:
        if r['_match'] or not r['_base']:
            continue
        if r['_waiting']:
            observations.append({'종목':r['종목'],'코드':r['코드'],'단계':'현재봉 돌파·대금 충족',
                '확인할 조건':'다음 완료봉 유지 확인',
                '대금(추정·억원)':round(r['_current_amount'],1),'동시간배수':round(r['_current_ratio'],2),
                '돌파선':round(r['_current_level'],2),'돌파봉종가':r['_current_close'],'돌파봉(KST)':r['기준봉(KST)'],
                '기준봉(KST)':r['기준봉(KST)']})
    return pd.DataFrame(observations)
