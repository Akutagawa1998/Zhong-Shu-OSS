"""Validated research records and bounded, metadata-only recovery views.

The immutable ledger remains the authority. Reviewed cards enrich named historical
updates; they never replace later events or verify a remote artifact implicitly.
"""
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

SEMANTIC_KINDS = {'exp', 'finding', 'decision', 'todo', 'import'}
RECORD_LIMIT = 48000
VIEW_LIMIT = 12000


def timestamp(value):
    if not isinstance(value, str): raise ValueError('timestamp must be a string')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None: raise ValueError('timestamp must include a timezone')
    return parsed


def validate_record(r):
    try:
        def require(ok, message):
            if not ok: raise ValueError(message)
        def text(v, name, nullable=False):
            require((nullable and v is None) or (isinstance(v, str) and bool(v.strip()) and len(v) <= 4096), name+' must be a nonempty string'+(' or null' if nullable else ''))
        def obj(v, keys, name):
            require(isinstance(v, dict), name+' must be an object')
            require(set(keys) <= set(v), name+' missing keys: '+', '.join(sorted(set(keys)-set(v))))
        def strings(v, name):
            require(isinstance(v, list), name+' must be a list')
            for x in v: text(x, name)
        def integer(v, name):
            require(v is None or (type(v) is int and v >= 0), name+' must be nonnegative integer or null')
        def digest(v, name):
            require(v is None or (isinstance(v,str) and re.fullmatch(r'[0-9a-f]{64}',v)), name+' must be SHA256 or null')
        required=['schema_version','experiment_id','task_type','summary','purpose','lineage','steps','model','datasets','recipe','evaluation','findings','artifacts','legacy_refs','gaps','source_event_ids','as_of']
        obj(r,required,'record'); require(r['schema_version']==1,'unsupported schema_version')
        require(r['task_type'] in {'training','inference','analysis','engineering'},'invalid task_type')
        for k in ['experiment_id','summary','purpose','lineage']: text(r[k], k)
        require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',r['experiment_id'])),'invalid experiment_id')
        timestamp(r['as_of'])
        for k in ['legacy_refs','gaps','source_event_ids']: strings(r[k], k)
        obj(r['steps'],['unit','planned','completed'],'steps')
        require(r['steps']['unit'] in {'optimizer_updates','evaluation_calls','checks','not_applicable'},'invalid step unit')
        for k in ['planned','completed']: integer(r['steps'][k],'steps.'+k)
        if r['steps']['unit']=='not_applicable': require(r['steps']['planned'] is None and r['steps']['completed'] is None,'not_applicable steps must be null')
        obj(r['model'],['identity','base','checkpoint','seed'],'model')
        for k in ['identity','base','checkpoint']:
            if isinstance(r['model'][k],list): strings(r['model'][k],'model.'+k)
            else: text(r['model'][k],'model.'+k,True)
        require(r['model']['seed'] is None or type(r['model']['seed']) is int or (isinstance(r['model']['seed'],list) and all(type(x) is int for x in r['model']['seed'])),'model.seed must be integer/list or null')
        require(isinstance(r['datasets'],list),'datasets must be a list')
        for i,d in enumerate(r['datasets']):
            obj(d,['id','split','role','count','sha256','location'],f'dataset[{i}]')
            for k in ['id','split','role']: text(d[k],'dataset.'+k)
            integer(d['count'],'dataset.count');digest(d['sha256'],'dataset.sha256');text(d['location'],'dataset.location',True)
        if r['task_type'] in {'training','inference'}: require(bool(r['datasets']),'training/inference needs dataset entries; use explicit unknown fields, not omission')
        obj(r['recipe'],['method','config','code_revision','location'],'recipe')
        text(r['recipe']['method'],'recipe.method'); require(isinstance(r['recipe']['config'],dict),'recipe.config must be an object')
        for k in ['code_revision','location']:text(r['recipe'][k],'recipe.'+k,True)
        obj(r['evaluation'],['purpose','metrics','acceptance'],'evaluation')
        text(r['evaluation']['purpose'],'evaluation.purpose',True)
        require(isinstance(r['evaluation']['metrics'],(list,dict)),'evaluation.metrics must be list or object')
        if isinstance(r['evaluation']['metrics'],list):strings(r['evaluation']['metrics'],'evaluation.metrics')
        if not isinstance(r['evaluation']['acceptance'],dict):text(r['evaluation']['acceptance'],'evaluation.acceptance',True)
        require(isinstance(r['findings'],list),'findings must be a list')
        for f in r['findings']:
            obj(f,['summary','evidence_level','status','sources'],'finding')
            text(f['summary'],'finding.summary');strings(f['sources'],'finding.sources')
            require(f['evidence_level'] in {'observed','reported','inferred'},'finding evidence_level')
            require(f['status'] in {'accepted','reported','inconclusive','retracted'},'finding status')
            require(f['status']!='accepted' or bool(f['sources']),'accepted finding needs evidence pointers')
        require(isinstance(r['artifacts'],list),'artifacts must be a list')
        for a in r['artifacts']:
            obj(a,['role','platform','uri','sha256','availability','checked_at'],'artifact')
            for k in ['role','uri']:text(a[k],'artifact.'+k)
            require(a['platform'] in {'local','remote','torch','alice','145','github','hf'},'artifact platform')
            require(a['availability'] in {'observed_local','reported_remote','verified_remote','planned','missing'},'artifact availability')
            digest(a['sha256'],'artifact.sha256')
            if a['checked_at'] is not None:timestamp(a['checked_at'])
            if a['availability'] in {'observed_local','verified_remote'}:require(a['checked_at'] is not None,'observed artifact needs checked_at')
        require(bool(r['artifacts']) or bool(r['gaps']),'artifact locations or explicit gaps required')
        encoded=json.dumps(r,ensure_ascii=False,allow_nan=False)
        require(len(encoded)<=RECORD_LIMIT,f'record exceeds {RECORD_LIMIT:,} characters; move detail into evidence files and cite them')
    except (ValueError,TypeError,KeyError,OverflowError) as e:
        return str(e)
    return ''


def load_record_file(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>192000:raise ValueError('record must be a small regular JSON file')
    r=json.loads(p.read_text())
    problem=validate_record(r)
    if problem:raise ValueError(problem)
    return r


def cards(hub):
    directory=hub/'records'
    if not directory.exists():return []
    if directory.is_symlink():raise ValueError('records directory cannot be a symlink')
    result=[]
    for p in sorted(directory.glob('*.json')):
        r=load_record_file(p)
        suffix=re.search(r'--([0-9a-f]{12})\.json$',p.name)
        if suffix and hashlib.sha256(p.read_bytes()).hexdigest()[:12]!=suffix[1]:raise ValueError('reviewed card bytes changed: '+p.name)
        result.append((p,r))
    return result


def matching_card(event, reviewed):
    if 'research' in event:return {'kind':'embedded_event','path':None,'record':event['research']}
    candidates=[(p,r) for p,r in reviewed if event.get('event_id') in r['source_event_ids']]
    if not candidates:return None
    # Multiple reviewed cards can represent different tasks in one historical update.
    return {'kind':'reviewed_historical_snapshot','cards':[{'path':str(p.relative_to(p.parent.parent)),'record':r} for p,r in candidates],
            'limits':'Only the explicitly referenced source update is covered; not automatic authority for a later event.'}


def record_summary(match):
    if not match:return None
    rs=[match['record']] if 'record' in match else [c['record'] for c in match['cards']]
    return [{'task_type':r['task_type'],'steps':r['steps'],'as_of':r['as_of'],'gap_count':len(r['gaps'])} for r in rs]


def coverage_view(hub, events):
    reviewed=cards(hub);semantic=[e for e in events if e.get('kind') in SEMANTIC_KINDS]
    covered={sid for _,r in reviewed for sid in r['source_event_ids']};ids={e.get('event_id') for e in events}
    return {'ledger_events':len(events),'semantic_events':len(semantic),'structured_events':sum('research' in e for e in semantic),
            'reviewed_snapshot_cards':len(reviewed),'semantic_events_with_reviewed_snapshot':sum(e.get('event_id') in covered for e in semantic),
            'legacy_missing_record':sum('research' not in e and e.get('event_id') not in covered for e in semantic),
            'snapshot_source_ids_missing_from_ledger':sorted(covered-ids),
            'limits':'Schema/source pointers are not scientific verification, backup, remote availability or complete historical migration.'}


def bounded(view, list_key=None):
    if list_key:
        while len(json.dumps(view,ensure_ascii=False))>VIEW_LIMIT and view[list_key]:
            view[list_key].pop();view['truncated']=True
        return view
    if len(json.dumps(view,ensure_ascii=False))<=VIEW_LIMIT:return view
    return {'error':f'Selected view exceeds {VIEW_LIMIT:,} characters; use --section record, sources or history, an exact event_id, or read one field in pages with --field NAME --page N (for example --field research).',
            'identifier':view.get('identifier'),'available_events':view.get('history',[])[:5],
            'record_file_paths':view.get('record_file_paths',[]), 'source_file_paths':view.get('source_file_paths',[]),
            'limits':'No scientific fields were silently dropped. Read the named small record or a narrower section.'}


def text_matcher(args):
    """(hits, several): hits(text) names the --query texts (and 'regex') that match; None without text filters.
    Several filters are alternatives (OR) that share one -n and one output budget."""
    queries=args.query if isinstance(args.query,list) else [args.query] if args.query else []
    pattern=getattr(args,'regex',None)
    if not queries and pattern is None:return None,False
    folded=[(q,q.casefold()) for q in queries]
    def hits(text):
        lowered=text.casefold()
        found=[q for q,f in folded if f in lowered]
        if pattern is not None and pattern.search(text):found.append('regex')
        return found
    return hits,len(queries)+(pattern is not None)>1


def catalog_view(hub, events, args):
    reviewed=cards(hub);matcher,several=text_matcher(args);groups={};selected=[]
    if any(e.get('kind')=='CORRUPT' for e in events):raise ValueError('Ledger is corrupt; run check before recovery')
    for ordinal,e in enumerate(events,1):
        if e.get('kind') not in SEMANTIC_KINDS:continue
        match=matching_card(e,reviewed)
        related=[r for _,r in reviewed if r['experiment_id']==e.get('id') and e.get('event_id') not in r['source_event_ids']]
        haystack=json.dumps({'id':e.get('id'),'event_id':e.get('event_id'),'source_id':e.get('source_id'),
                            'source_namespace':e.get('source_namespace'),'note':e.get('note'),'record':match,'related_records':related},ensure_ascii=False)
        found=matcher(haystack) if matcher else []
        hit=(not args.thread or e.get('thread')==args.thread) and (not matcher or bool(found))
        row={'sequence':e.get('sequence',ordinal),'sequence_source':'stored' if 'sequence' in e else 'derived_current_ledger_position',
             'event_id':e.get('event_id'),'experiment_id':e.get('id'),'source_namespace':e.get('source_namespace'),
             'source_id':e.get('source_id'),
             'updated':e.get('ts'),'thread':e.get('thread'),'status':e.get('status'),
             'summary':(e.get('research',{}).get('summary') or e.get('note') or '')[:320],
             'record':record_summary(match),'structured':bool(match),
             'related_records':[{'as_of':r['as_of'],'source_event_ids':r['source_event_ids']} for r in related]}
        if 'body_chars' in e:row['body_chars']=e['body_chars']
        key=(e.get('source_namespace'),e.get('id') or e.get('event_id'))
        groups.setdefault(key,[]).append((row,hit,found))
        if args.updates and hit:selected.append(dict(row,matched_by=found) if several else row)
    if not args.updates:
        for history in groups.values():
            matches=[(r,found) for r,hit,found in history if hit]
            if not matches:continue
            # Pick latest by immutable append order BEFORE searching. A keyword
            # found only in old positive evidence must not hide a retraction.
            row=dict(history[-1][0])
            if matcher or args.thread:
                row['matched_event_ids']=[r['event_id'] for r,_ in matches][-5:]
                row['latest_update_matches_filter']=history[-1][1]
            if several:row['matched_by']=list(dict.fromkeys(x for _,found in matches for x in found))
            selected.append(row)
    represented={key[1] for key in groups}
    for path,r in reviewed:
        if r['experiment_id'] in represented:continue
        refs=[e for e in events if e.get('event_id') in r['source_event_ids']]
        if args.thread and not any(e.get('thread')==args.thread for e in refs):continue
        found=matcher(json.dumps(r,ensure_ascii=False)) if matcher else []
        if matcher and not found:continue
        selected.append({'sequence':None,'sequence_source':'reviewed_card_not_new_execution',
             'event_id':None,'source_event_ids':r['source_event_ids'],'experiment_id':r['experiment_id'],
             'updated':r['as_of'],'thread':refs[-1].get('thread') if refs else None,'status':'reviewed_snapshot',
             'summary':r['summary'][:320],'record':[{'task_type':r['task_type'],'steps':r['steps'],'as_of':r['as_of'],'gap_count':len(r['gaps'])}],
             'record_file':str(path.relative_to(hub)),'structured':True,**({'matched_by':found} if several else {})})
    # Zoned timestamps are compared as instants, solely for display order.
    selected.sort(key=lambda row:timestamp(row['updated']))
    view={'rows':list(reversed(selected))[:args.n],'matching_count':len(selected),'truncated':len(selected)>args.n,
          'sequence_rule':'event_id is stable identity. Legacy sequence is current ledger position, not optimizer steps or a distributed counter.',
          'latest_rule':'Default returns latest appended update per ID/namespace even when only older text matches. --updates returns individual matching updates.',
          'next':'rmem show <experiment_id-or-event_id>; source_namespace conflicts require event UUID. No repository content scan.'}
    return bounded(view,'rows')


def source_locations(hub, event):
    from artifact_routes import resolve_artifact
    root_file=hub/'sources/roots.json';roots=json.loads(root_file.read_text()) if root_file.is_file() else []
    roots=sorted(roots,key=lambda x:len(x.get('path','')),reverse=True)
    overrides_file=hub/'local-roots.json';overrides=json.loads(overrides_file.read_text()) if overrides_file.is_file() else {}
    provenance_file=hub/'sources/selected-provenance.json'
    snapshots=json.loads(provenance_file.read_text()).get('sources',[]) if provenance_file.is_file() else []
    mapped=[]
    for s in event.get('sources',[]):
        item=dict(s);original=Path(s['path']);candidates=[];logical=None;project_source=False
        for root in roots:
            rp=Path(root['path'])
            if original.is_relative_to(rp):
                rel=original.relative_to(rp);logical={'root_id':root['id'],'relative_path':str(rel)}
                project_source=(root.get('project_relative')=='.' or rp.resolve()==hub.parent.resolve())
                if project_source:candidates.append(hub.parent/rel)
                if root['id'] in overrides:candidates.append(Path(overrides[root['id']]).expanduser()/rel)
                break
        candidates.append(original)
        item['logical_location']=logical
        if logical and project_source:
            try:
                route=resolve_artifact(hub.parent, logical['relative_path'], s.get('sha256'),
                                       manifest=hub/'ARTIFACT_ROUTES.json')
                if route:item['relocated_artifact']=route
            except (OSError, ValueError, KeyError) as error:
                item['relocation_error']=str(error)
        item['local_candidates']=[{'path':str(p),'exists':p.is_file()} for p in dict.fromkeys(candidates)]
        matching=[x for x in snapshots if x.get('sha256')==s.get('sha256') and x.get('snapshot')]
        item['snapshot_candidates']=[{'path':str(hub/x['snapshot']),'exists':(hub/x['snapshot']).is_file(),'expected_sha256':s['sha256']} for x in matching]
        item['verification']='Existence only; verify expected SHA before scientific use. Snapshot is historical, never a live controller.'
        mapped.append(item)
    return mapped


def event_history(events, identifier):
    """(history, exact, historical_source_lookup) for an event UUID, experiment ID or historical import source ID."""
    if any(e.get('kind')=='CORRUPT' for e in events):raise ValueError('Ledger is corrupt; run check before recovery')
    exact=[e for e in events if e.get('event_id')==identifier]
    history=exact or [e for e in events if e.get('id')==identifier]
    historical_source_lookup=False
    if not history:
        imports=[e for e in events if e.get('kind')=='import' and e.get('source_id')==identifier]
        if len({e.get('source_namespace') for e in imports})>1:
            raise ValueError('Historical source ID spans multiple source namespaces; choose exact ingestion event_id from catalog')
        if imports:
            history=imports
            historical_source_lookup=True
    if history:
        if len(exact)>1:raise ValueError('Duplicate event ID; repair ledger before recovery')
        if not exact and len({e.get('source_namespace') for e in history})>1:raise ValueError('Experiment ID spans multiple source namespaces; choose exact event_id from catalog')
    return history, exact, historical_source_lookup


def show_view(hub, events, identifier, section):
    history, exact, historical_source_lookup = event_history(events, identifier)
    if not history:
        matches=[(p,r) for p,r in cards(hub) if r['experiment_id']==identifier]
        if not matches:raise ValueError('Unknown exact experiment/event ID; use catalog --query. No historical whole-repository fallback.')
        view={'identifier':identifier,'event_id':None,'section':section,
            'record_file_paths':[str(p.relative_to(hub)) for p,_ in matches],
            'limits':'This frozen experiment ID differs from hub update IDs or has no imported event. Follow source_event_ids and legacy_refs; no new run or acceptance is implied.'}
        if section in {'all','record'}:
            view['record']={'kind':'reviewed_historical_snapshot','cards':[{'path':str(p.relative_to(hub)),'record':r} for p,r in matches]}
        if section in {'all','history'}:
            view['history']=[{'source_event_ids':r['source_event_ids'],'legacy_refs':r['legacy_refs'],'as_of':r['as_of']} for _,r in matches]
        if section in {'all','sources'}:
            view['sources']=[{'record_file':str(p.relative_to(hub)),'artifacts':r['artifacts']} for p,r in matches]
        return bounded(view)
    latest=history[-1];reviewed=cards(hub)
    match=matching_card(latest,reviewed)
    hs=[{'event_id':e.get('event_id'),'time':e.get('ts'),'kind':e.get('kind'),'status':e.get('status'),'supersedes':e.get('supersedes')} for e in history]
    result={'identifier':identifier,'event_id':latest.get('event_id'),'experiment_id':latest.get('id'),'section':section,
            'record_file_paths':[str(p.relative_to(hub)) for p,r in reviewed if r['experiment_id']==latest.get('id') or latest.get('event_id') in r['source_event_ids']],
            'source_file_paths':[x['path'] for x in latest.get('sources',[])][:20],
            'limits':'Latest exact ID update, not a merged scientific conclusion. Related cohort IDs and corrections must be followed explicitly.'}
    if historical_source_lookup:
        result.update(lookup_kind='historical_import_source_id', original_source_id=identifier,
                      source_namespace=latest.get('source_namespace'), ingestion_event_id=latest.get('event_id'))
        result['limits']='Historical import located by original source ID. event_id and ingestion_event_id identify the new ledger ingestion, not the original event. Follow the immutable sources for the original body; no historical claim, execution or acceptance is replayed.'
    if section in {'all','history'}:result['history']=hs[-20:];result['history_total']=len(hs)
    if section in {'all','record'}:
        result['record']=match
        if not match:result['record_gap']='No structured record for this exact update. Use cited evidence and append a reviewed card; do not infer absent recipe/steps.'
        related=[{'path':str(p.relative_to(hub)),'record':r} for p,r in reviewed if r['experiment_id']==latest.get('id') and latest.get('event_id') not in r['source_event_ids']]
        if related:
            result['related_reviewed_records']=related
            result['related_record_limits']='These cards cite other exact updates, possibly a later acceptance under a different event ID. They do not fill this selected event record gap or override it; follow source_event_ids/as_of.'
    if section in {'all','sources'}:
        result['sources']=source_locations(hub,latest);result['artifacts']=latest.get('artifacts',[])
    if section=='all':result['event']={k:v for k,v in latest.items() if k not in {'sources','research'}}
    stored=[p for p in ('body','cmd') if latest.get(p+'_path')]
    if stored:result['full_text']=[f"rmem show {latest.get('event_id') or identifier} --field {p}" for p in stored]
    view=bounded(result)
    if historical_source_lookup and 'lookup_kind' not in view:
        for key in ('lookup_kind','original_source_id','source_namespace','ingestion_event_id','event_id'):
            view[key]=result[key]
        view['lookup_limits']=result['limits']
    return view
