"""Pure boundary recognition: observations are evidence, not reset events."""


def observe(state, reset, duration, observed):
    """Return updated JSON state and whether a stable candidate may open a cycle.

    Times are Unix seconds. Duplicate/stale observations do not establish stability.
    A changed boundary cannot truncate an unexpired, established cycle.
    """
    state=dict(state or {})
    if observed<=state.get('observed_at',-1):return state,False
    state['observed_at']=observed
    current=state.get('current')
    if current and current['duration']==duration and abs(current['reset']-reset)<=1:
        state.pop('candidate',None)
        state['status']='available' if observed<current['reset'] else 'waiting_new_cycle'
        return state,False
    candidate=dict(state.get('candidate') or {})
    if candidate.get('duration')!=duration or abs(candidate.get('reset',-100)-reset)>1:
        candidate=dict(reset=reset,duration=duration,first=observed,count=1)
    else:
        candidate['count']+=1
    state['candidate']=candidate
    stable=candidate['count']>=3 and observed-candidate['first']>=60
    state['status']='boundary_pending'
    if not stable:return state,False
    start=candidate['reset']-duration*60
    if current and (observed<current['reset'] or start<current['reset']):
        state['status']='boundary_review'
        return state,False
    state['status']='available'
    return state,True
