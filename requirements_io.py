"""File loaders and validated LOT ORSD/CQ inputs from the attached working project."""
import json
import re
import hashlib
from pathlib import Path

def require_text(value, location):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{location} must be a nonempty string.')

def require_id(value, location):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError(f'{location} needs an id of 1..80 letters, digits, underscores or hyphens.')

def require_text_list(value, location):
    if not isinstance(value, list) or not all((isinstance(x, str) and x.strip() for x in value)):
        raise ValueError(f'{location} must be a list of nonempty strings.')

def validate_cqs(cqs):
    if not isinstance(cqs, list) or not cqs:
        raise ValueError('Supply a nonempty list of CQs.')
    seen = set()
    allowed = {'id', 'question', 'expected_answer', 'notes', 'use_case_ids'}
    for cq in cqs:
        if not isinstance(cq, dict) or set(cq) - allowed:
            raise ValueError(f'Each CQ must be an object with fields from {sorted(allowed)}.')
        cid = cq.get('id')
        require_id(cid, 'CQ.id')
        if cid in seen:
            raise ValueError(f'Duplicate CQ id: {cid}')
        seen.add(cid)
        require_text(cq.get('question'), f'{cid}.question')
        for key in ('expected_answer', 'notes'):
            if key in cq and (not isinstance(cq[key], str)):
                raise ValueError(f'{cid}.{key} must be a string.')
        require_text_list(cq.get('use_case_ids', []), f'{cid}.use_case_ids')

def validate_orsd(orsd, cqs):
    """Validate this notebook's JSON representation, not an official LOT schema."""
    validate_cqs(cqs)
    if not isinstance(orsd, dict):
        raise ValueError('ORSD must be a dictionary, or load a JSON object from a file.')
    allowed = {'metadata', 'purpose', 'scope', 'intended_users', 'use_cases', 'data_exchange_specification', 'functional_requirements', 'non_functional_requirements', 'terminology', 'modelling_decisions', 'open_questions', 'reuse_context', 'additional_context'}
    if set(orsd) - allowed:
        raise ValueError(f'Unknown ORSD sections: {sorted(set(orsd) - allowed)}. Put extra prose in additional_context.')
    metadata = orsd.get('metadata')
    if not isinstance(metadata, dict) or set(metadata) - {'id', 'title', 'version', 'status'}:
        raise ValueError('ORSD.metadata requires id/title/version/status; no other fields.')
    require_id(metadata.get('id'), 'ORSD.metadata.id')
    for key in ('title', 'version', 'status'):
        require_text(metadata.get(key), f'ORSD.metadata.{key}')
    require_text(orsd.get('purpose'), 'ORSD.purpose')
    scope = orsd.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'included', 'excluded'}:
        raise ValueError('ORSD.scope requires included and excluded lists.')
    for key in scope:
        require_text_list(scope[key], f'ORSD.scope.{key}')
    if not scope['included']:
        raise ValueError('ORSD.scope.included needs at least one item.')
    require_text_list(orsd.get('intended_users', []), 'ORSD.intended_users')
    for key in ('reuse_context', 'additional_context'):
        if not isinstance(orsd.get(key, ''), str):
            raise ValueError(f'ORSD.{key} must be a string.')
    cq_ids = {cq['id'] for cq in cqs}
    seen = set(cq_ids)

    def register(item, section):
        require_id(item.get('id'), f'ORSD.{section}.id')
        if item['id'] in seen:
            raise ValueError(f"Duplicate requirement or document id: {item['id']}")
        seen.add(item['id'])
    register(metadata, 'metadata')
    for section in ('use_cases', 'data_exchange_specification', 'functional_requirements', 'non_functional_requirements', 'terminology', 'modelling_decisions', 'open_questions'):
        entries = orsd.get(section, [])
        if not isinstance(entries, list) or not all((isinstance(item, dict) for item in entries)):
            raise ValueError(f'ORSD.{section} must be a list of objects.')
        for item in entries:
            if section == 'terminology':
                if set(item) != {'term', 'definition'}:
                    raise ValueError('Terminology entries require term and definition.')
                require_text(item['term'], 'terminology.term')
                require_text(item['definition'], 'terminology.definition')
                continue
            register(item, section)
            if section == 'use_cases':
                if set(item) != {'id', 'title', 'description'}:
                    raise ValueError('Use cases require id/title/description.')
                require_text(item['title'], f"{item['id']}.title")
                require_text(item['description'], f"{item['id']}.description")
            elif section == 'data_exchange_specification':
                if set(item) != {'id', 'description', 'sources', 'fields'}:
                    raise ValueError('Data exchanges require id/description/sources/fields.')
                require_text(item['description'], f"{item['id']}.description")
                require_text_list(item['sources'], f"{item['id']}.sources")
                if not isinstance(item['fields'], list):
                    raise ValueError(f"{item['id']}.fields must be a list.")
                for field in item['fields']:
                    if not isinstance(field, dict) or set(field) != {'name', 'value_type', 'description'}:
                        raise ValueError('Data fields require name/value_type/description.')
                    for key, value in field.items():
                        require_text(value, f"{item['id']}.fields.{key}")
            else:
                kind = item.get('type', 'statement')
                is_table = section == 'functional_requirements' and kind == 'table'
                allowed_fields = {'id', 'text', 'applies_to'}
                if section == 'functional_requirements':
                    allowed_fields |= {'type'}
                    if kind not in {'statement', 'table'}:
                        raise ValueError(f"{item['id']}.type must be statement or table.")
                    if is_table:
                        allowed_fields |= {'columns', 'rows'}
                if set(item) - allowed_fields:
                    raise ValueError(f"Unsupported fields in {item['id']}: {sorted(set(item) - allowed_fields)}")
                require_text(item.get('text'), f"{item['id']}.text")
                refs = item.get('applies_to', [])
                require_text_list(refs, f"{item['id']}.applies_to")
                if set(refs) - cq_ids:
                    raise ValueError(f"{item['id']} refers to unknown CQ ids: {sorted(set(refs) - cq_ids)}")
                if is_table:
                    columns = item.get('columns')
                    require_text_list(columns, f"{item['id']}.columns")
                    if not columns or len(columns) != len(set(columns)):
                        raise ValueError(f"{item['id']}.columns must be nonempty and unique.")
                    rows = item.get('rows')
                    if not isinstance(rows, list) or not rows:
                        raise ValueError(f"{item['id']}.rows must be a nonempty list.")
                    for row in rows:
                        if not isinstance(row, dict) or set(row) != set(columns):
                            raise ValueError(f"Each {item['id']} row must have exactly the specified columns.")
                        if any((value is not None and (not isinstance(value, (str, int, float, bool))) for value in row.values())):
                            raise ValueError(f"{item['id']} table cells must be scalar JSON values.")
    use_ids = {item['id'] for item in orsd.get('use_cases', [])}
    for cq in cqs:
        if set(cq.get('use_case_ids', [])) - use_ids:
            raise ValueError(f"{cq['id']} refers to an unknown use case.")
    return orsd

def select_cqs(cqs, ids=None):
    validate_cqs(cqs)
    if ids is None:
        return list(cqs)
    require_text_list(ids, 'CQ_IDS')
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('CQ_IDS must be nonempty and unique, or None for all CQs.')
    available = {cq['id']: cq for cq in cqs}
    unknown = set(ids) - set(available)
    if unknown:
        raise ValueError(f'Unknown CQ ids: {sorted(unknown)}')
    return [available[cid] for cid in ids]

def scope_orsd(orsd, cqs):
    """Keep shared context and global requirements; filter only explicit CQ-specific records."""
    scoped = json.loads(json.dumps(orsd, ensure_ascii=False))
    ids = {cq['id'] for cq in cqs}
    for section in ('functional_requirements', 'non_functional_requirements', 'modelling_decisions', 'open_questions'):
        scoped[section] = [item for item in scoped.get(section, []) if not item.get('applies_to') or ids.intersection(item['applies_to'])]
    return scoped

def read_json_file(path, raw=None):
    """Load UTF-8 JSON with actionable errors; reject duplicate keys and non-JSON numbers."""
    path = Path(path)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    try:
        return json.loads((path.read_bytes() if raw is None else raw).decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f'Invalid JSON constant: {value}')))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot load JSON file {path}: {exc}') from exc


def load_files(settings_path, config_type):
    """Load one fresh input snapshot; all relative paths use the settings directory."""
    from vocabulary import Catalogue, valid_iri
    settings_path = Path(settings_path).expanduser().resolve()
    try:
        contents = {"settings": settings_path.read_bytes()}
    except OSError as exc:
        raise ValueError(f"Cannot read settings file {settings_path}: {exc}") from exc
    settings = read_json_file(settings_path, contents["settings"])
    if not isinstance(settings, dict) or set(settings) != {"generation", "files", "cq_ids", "output"}:
        raise ValueError("Settings require generation/files/cq_ids/output.")
    if not isinstance(settings["generation"], dict):
        raise ValueError("generation must be an object.")
    try:
        config = config_type(**settings["generation"])
    except TypeError as exc:
        raise ValueError(f"Invalid generation setting: {exc}") from exc
    config.validate()
    expected = {"cqs", "orsd", "prompt", "catalogue"}
    if not isinstance(settings["files"], dict) or set(settings["files"]) != expected:
        raise ValueError(f"files requires exactly {sorted(expected)}.")
    paths = {"settings": settings_path}
    for role, filename in settings["files"].items():
        require_text(filename, f"files.{role}")
        path = Path(filename).expanduser()
        path = (settings_path.parent / path).resolve() if not path.is_absolute() else path.resolve()
        paths[role] = path
        try:
            contents[role] = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Cannot read {role} file {path}: {exc}") from exc
    cqs = read_json_file(paths["cqs"], contents["cqs"])
    orsd = read_json_file(paths["orsd"], contents["orsd"])
    validate_orsd(orsd, cqs)
    selected = select_cqs(cqs, settings["cq_ids"])
    prompt = contents["prompt"].decode("utf-8-sig")
    require_text(prompt, "Prompt file")
    catalogue = Catalogue(read_json_file(paths["catalogue"], contents["catalogue"]))
    output = settings["output"]
    if not isinstance(output, dict) or set(output) != {"file", "ontology_iri", "overwrite"}:
        raise ValueError("output requires file/ontology_iri/overwrite.")
    require_text(output["file"], "output.file")
    require_text(output["ontology_iri"], "output.ontology_iri")
    if not isinstance(output["overwrite"], bool):
        raise ValueError("output.overwrite must be a boolean.")
    if not valid_iri(output["ontology_iri"]) or "/lumis" in output["ontology_iri"].lower() or output["ontology_iri"].startswith(tuple(catalogue.prefixes.values())):
        raise ValueError("Use a neutral ontology document IRI outside vocabulary namespaces.")
    output_path = Path(output["file"]).expanduser()
    output_path = (settings_path.parent / output_path).resolve() if not output_path.is_absolute() else output_path.resolve()
    if output_path.suffix.lower() not in {".owl", ".rdf", ".xml", ".ttl"}:
        raise ValueError("Output file must end in .owl/.rdf/.xml (RDF/XML) or .ttl (Turtle).")
    if output_path in paths.values():
        raise ValueError("Output must not overwrite an input file.")
    hashes = {role: hashlib.sha256(data).hexdigest() for role, data in contents.items()}
    return {"config": config, "cqs": cqs, "selected_cqs": selected, "orsd": orsd,
            "prompt": prompt, "catalogue": catalogue, "output_file": output_path,
            "ontology_iri": output["ontology_iri"], "overwrite": output["overwrite"],
            "paths": paths, "input_hashes": hashes}
