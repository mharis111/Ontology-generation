"""Existing vocabulary terms only; no local class or property constructors."""
import copy
import json
import re
from urllib.parse import urlparse

from rdflib import Graph, URIRef, BNode, Literal, RDF, RDFS, OWL, XSD, Namespace
from rdflib.collection import Collection

SH = Namespace("http://www.w3.org/ns/shacl#")
DC = Namespace("http://purl.org/dc/terms/")
STANDARD = {"rdf": str(RDF), "rdfs": str(RDFS), "owl": str(OWL), "xsd": str(XSD), "sh": str(SH)}
DATATYPES = ["string", "integer", "nonNegativeInteger", "decimal", "float", "double", "boolean", "date", "dateTime", "anyURI", "langString"]


def valid_iri(value):
    if not isinstance(value, str) or re.search(r'[\s<>"\\]', value):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate model JSON key: {key}")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"Invalid JSON constant: {x}")))


class Catalogue:
    def __init__(self, data):
        required = {"catalogue_version", "reviewed_on", "policy", "prefixes", "terms"}
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError(f"Catalogue requires {sorted(required)}.")
        if not isinstance(data["prefixes"], dict) or not isinstance(data["terms"], list):
            raise ValueError("Catalogue prefixes must be an object and terms a list.")
        self.data = copy.deepcopy(data)
        self.prefixes = data["prefixes"]
        for prefix, iri in self.prefixes.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", prefix) or prefix in STANDARD or prefix.lower() == "lumis" or not valid_iri(iri) or iri.startswith("https://example.org/"):
                raise ValueError(f"Invalid or local vocabulary namespace: {prefix}")
        self.terms, self.by_iri = {}, {}
        fields = {"curie", "iri", "kind", "value_kind", "label", "definition", "usage_note", "source", "source_version", "declaration"}
        for term in data["terms"]:
            if not isinstance(term, dict) or not fields <= set(term) or set(term) - fields - {"parents", "domain", "range", "inverse"}:
                raise ValueError("Invalid catalogue term fields.")
            for key in fields - {"value_kind"}:
                if not isinstance(term[key], str) or (not term[key].strip() and key != "usage_note"):
                    raise ValueError(f"Invalid catalogue term {key}.")
            curie = term["curie"]
            if curie.count(":") != 1:
                raise ValueError(f"Use an exact prefixed catalogue term: {curie}")
            prefix, local = curie.split(":")
            if prefix not in self.prefixes or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", local) or term["iri"] != self.prefixes[prefix] + local:
                raise ValueError(f"Catalogue prefix/IRI mismatch: {curie}")
            if not valid_iri(term["iri"]) or not valid_iri(term["source"]):
                raise ValueError(f"Invalid term/source IRI: {curie}")
            if term["kind"] == "class":
                if term["value_kind"] is not None or term["declaration"] not in {"owl:Class", "rdfs:Class"}:
                    raise ValueError(f"Invalid class declaration: {curie}")
            elif term["kind"] == "property":
                allowed = {"resource": {"owl:ObjectProperty", "rdf:Property"},
                           "literal": {"owl:DatatypeProperty", "owl:AnnotationProperty", "rdf:Property"}}
                if term["value_kind"] not in allowed or term["declaration"] not in allowed[term["value_kind"]]:
                    raise ValueError(f"Invalid property usage/declaration: {curie}")
            else:
                raise ValueError(f"Unknown catalogue kind: {curie}")
            if curie in self.terms or term["iri"] in self.by_iri:
                raise ValueError(f"Duplicate catalogue term: {curie}")
            self.terms[curie] = term
            self.by_iri[term["iri"]] = curie
        for curie, term in self.terms.items():
            parents = term.get("parents", [])
            if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
                raise ValueError(f"Invalid catalogue parents: {curie}")
            for ref in parents + [term[k] for k in ("domain", "range") if k in term]:
                if not isinstance(ref, str) or self.terms.get(ref, {}).get("kind") != "class":
                    raise ValueError(f"Unknown catalogue class {ref} in {curie}")
            if "inverse" in term:
                other = self.terms.get(term["inverse"], {})
                if term["value_kind"] != "resource" or other.get("value_kind") != "resource" or other.get("inverse") != curie:
                    raise ValueError(f"Invalid catalogue inverse pair: {curie}")

    def canonical(self, value):
        if isinstance(value, str) and value in self.by_iri:
            return self.by_iri[value]
        if not isinstance(value, str) or value not in self.terms:
            raise ValueError(f"Unapproved term {value!r}. Only exact existing catalogue terms are allowed. Do not create local classes/properties or an IRI class; select a suitable approved term or report an unresolved requirement.")
        return value

    def node(self, value):
        return URIRef(self.terms[self.canonical(value)]["iri"])

    def subclass(self, child, parent, visited=None):
        if child == parent:
            return True
        visited = set() if visited is None else visited
        if child in visited:
            return False
        visited.add(child)
        return any(self.subclass(p, parent, visited.copy()) for p in self.terms[child].get("parents", []))

    def expand(self, selected):
        result = set(selected)
        changed = True
        while changed:
            changed = False
            for name in list(result):
                term = self.terms[name]
                for parent in term.get("parents", []) + [term[k] for k in ("domain", "range") if k in term]:
                    if parent not in result:
                        result.add(parent); changed = True
        return sorted(result)

    def prompt_data(self):
        return {"prefixes": self.prefixes, "terms": [{k: t[k] for k in
                ("curie", "kind", "value_kind", "definition", "usage_note", "parents", "domain", "range") if k in t}
                for t in self.terms.values()]}


def build_schema(catalogue):
    def obj(fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    array = lambda item: {"type": "array", "items": item}
    classes = sorted(k for k, t in catalogue.terms.items() if t["kind"] == "class")
    properties = sorted(k for k, t in catalogue.terms.items() if t["kind"] == "property")
    if not classes or not properties:
        raise ValueError("Catalogue needs at least one class and one property.")
    term = lambda choices: {"type": "string", "enum": choices}
    # Encode the property/value dependency in the provider schema itself.
    # A single generic item schema let dcterms:identifier select value_classes.
    # These disjoint branches still need the cross-reference validator below.
    usage_branches = []
    for value_kind in ("resource", "literal"):
        choices = sorted(k for k in properties if catalogue.terms[k]["value_kind"] == value_kind)
        if not choices:
            continue
        usage_branches.append(obj({
            "property": term(choices),
            "on_class": term(classes),
            "value_classes": {**array(term(classes)),
                              **({"minItems": 1} if value_kind == "resource" else {"maxItems": 0})},
            "datatypes": {**array(term(DATATYPES)),
                          **({"minItems": 1} if value_kind == "literal" else {"maxItems": 0})},
        }))
    result = obj({
        "selected_terms": {**array(term(sorted(catalogue.terms))), "minItems": 1},
        "property_uses": {**array({"anyOf": usage_branches}), "minItems": 1},
        "notes": array({"type": "string", "minLength": 1}),
        "unresolved_requirements": array(obj({"requirement_id": {"type": "string", "minLength": 1},
                                               "reason": {"type": "string", "minLength": 1}})),
    })
    return result


def empty_profile():
    return {"selected_terms": [], "property_uses": [], "notes": [], "unresolved_requirements": []}


class PropertyValueError(ValueError):
    """A repairable resource/literal mismatch; no ontology content is auto-corrected."""

    def __init__(self, index, item, term):
        self.location = f"property_uses[{index}]"
        self.property = item["property"]
        self.on_class = item["on_class"]
        self.value_kind = term["value_kind"]
        if self.value_kind == "literal":
            detail = ("uses literal values: use datatypes, not class references. "
                      "Set value_classes to [] and datatypes to supported datatype names, "
                      "for example [\"string\"] for a plain identifier. "
                      "The resource receiving the identifier belongs in on_class, not value_classes.")
        else:
            detail = ("links resources: use value_classes, not literal datatypes. "
                      "Set datatypes to [] and value_classes to suitable selected existing class IDs.")
        super().__init__(f"{self.location}: {self.property} {detail}")

    def repair_hint(self):
        if self.value_kind == "literal":
            record = {"property": self.property, "on_class": self.on_class,
                      "value_classes": [], "datatypes": ["string"]}
            return ("Literal-valued usage shape for the failing property (string is an example; "
                    "choose the datatype(s) required by the requirements):\n" + json.dumps(record)
                    + "\nDo not invent an Identifier, IRI or string class to bypass this error. "
                    "Keep identifiers literal in this application profile.")
        return ("Keep the existing property and on_class; clear datatypes and supply appropriate "
                "selected existing value_classes. Do not substitute a different property to bypass validation.")


def validate_profile(raw, catalogue, requirement_ids):
    model = json_object(raw) if isinstance(raw, str) else copy.deepcopy(raw)
    if not isinstance(model, dict) or set(model) != set(empty_profile()) or not all(isinstance(v, list) for v in model.values()):
        raise ValueError("Return exactly selected_terms, property_uses, notes and unresolved_requirements arrays. Local class/property definition arrays are forbidden.")
    selected = [catalogue.canonical(v) for v in model["selected_terms"]]
    if len(selected) != len(set(selected)):
        raise ValueError("selected_terms contains duplicates.")
    if not any(catalogue.terms[v]["kind"] == "class" for v in selected):
        raise ValueError("Select at least one existing class.")
    model["selected_terms"] = sorted(selected)
    if not model["property_uses"]:
        raise ValueError("Supply at least one meaningful property usage.")
    seen = set()
    def ref(value, kind):
        name = catalogue.canonical(value)
        if name not in selected:
            raise ValueError(f"Add {name} to selected_terms before referencing it.")
        if catalogue.terms[name]["kind"] != kind:
            raise ValueError(f"{name} must be used as {catalogue.terms[name]['kind']}, not {kind}.")
        return name
    for index, item in enumerate(model["property_uses"]):
        if not isinstance(item, dict) or set(item) != {"property", "on_class", "value_classes", "datatypes"}:
            raise ValueError("Each property use needs property/on_class/value_classes/datatypes.")
        item["property"] = ref(item["property"], "property")
        item["on_class"] = ref(item["on_class"], "class")
        if not isinstance(item["value_classes"], list) or not isinstance(item["datatypes"], list):
            raise ValueError("value_classes and datatypes must be lists.")
        term = catalogue.terms[item["property"]]
        # Explain the structural error before trying to resolve a datatype such
        # as "string" incorrectly placed in the class-reference field.
        if term["value_kind"] == "resource" and (not item["value_classes"] or item["datatypes"]):
            raise PropertyValueError(index, item, term)
        if term["value_kind"] == "literal" and (not item["datatypes"] or item["value_classes"]):
            raise PropertyValueError(index, item, term)
        item["value_classes"] = sorted({ref(v, "class") for v in item["value_classes"]})
        if not all(isinstance(v, str) and v in DATATYPES for v in item["datatypes"]):
            raise ValueError(f"Datatypes must be supported local names: {DATATYPES}")
        item["datatypes"] = sorted(set(item["datatypes"]))
        pair = (item["property"], item["on_class"])
        if pair in seen:
            raise ValueError(f"Duplicate property/class usage: {pair}. Put alternatives in one value_classes or datatypes list.")
        seen.add(pair)
        if "domain" in term and not catalogue.subclass(item["on_class"], term["domain"]):
            raise ValueError(f"{item['property']} requires a compatible {term['domain']} domain in this catalogue. A source graph is not automatically a Catalog.")
        for value in item["value_classes"]:
            if "range" in term and not catalogue.subclass(value, term["range"]):
                raise ValueError(f"{item['property']} requires values compatible with {term['range']}.")
    for note in model["notes"]:
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Notes must be nonempty text.")
    for item in model["unresolved_requirements"]:
        if not isinstance(item, dict) or set(item) != {"requirement_id", "reason"} or item["requirement_id"] not in requirement_ids or not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ValueError("Unresolved requirements need a known active requirement_id and a reason.")
    return model


def merge_profiles(left, right, *, allow_union=True):
    """Combine selected vocabulary and usage alternatives without intersecting shared domains."""
    result = copy.deepcopy(left)
    result["selected_terms"] = sorted(set(left["selected_terms"]) | set(right["selected_terms"]))
    uses = {(u["property"], u["on_class"]): u for u in result["property_uses"]}
    for item in right["property_uses"]:
        pair = (item["property"], item["on_class"])
        if pair in uses:
            if not allow_union and any(set(uses[pair][key]) != set(item[key]) for key in ("value_classes", "datatypes")):
                raise ValueError(f"Incremental usage {pair} changed its value constraints. Preserve the prior usage or regenerate in joint mode.")
            for key in ("value_classes", "datatypes"):
                uses[pair][key] = sorted(set(uses[pair][key]) | set(item[key]))
        else:
            uses[pair] = copy.deepcopy(item)
    result["property_uses"] = [uses[key] for key in sorted(uses)]
    result["notes"] = list(dict.fromkeys(left["notes"] + right["notes"]))
    unresolved = {(u["requirement_id"], u["reason"]): u for u in left["unresolved_requirements"] + right["unresolved_requirements"]}
    result["unresolved_requirements"] = list(unresolved.values())
    return result


def build_graph(profile, catalogue, ontology_iri, metadata, cqs, generation_info):
    """One RDF graph: OWL vocabulary selection and anonymous SHACL usage constraints."""
    if not valid_iri(ontology_iri) or ontology_iri.startswith(tuple(catalogue.prefixes.values())):
        raise ValueError("Use a separate HTTP(S) ontology document IRI, not an external term namespace.")
    if "/lumis" in ontology_iri.lower():
        raise ValueError("Use a neutral ontology document IRI; this version mints no LUMIS vocabulary.")
    graph = Graph()
    for prefix, iri in {**STANDARD, **catalogue.prefixes}.items():
        graph.bind(prefix, iri, replace=True)
    root = URIRef(ontology_iri)
    graph.add((root, RDF.type, OWL.Ontology))
    graph.add((root, RDFS.label, Literal(metadata.get("title", "Scholarly discovery ontology profile"))))
    graph.add((root, OWL.versionInfo, Literal(generation_info.get("engine_version", "4.0.1") + "-draft")))
    graph.add((root, RDFS.comment, Literal("Existing vocabulary terms only. Embedded anonymous SHACL shapes record draft usage constraints; they do not redefine source properties' global domains/ranges. Logical consistency, CQ coverage and data conformance require separate evaluation.")))
    graph.add((root, DC.description, Literal("Run metadata: " + json.dumps(generation_info, sort_keys=True))))
    for cq in cqs:
        graph.add((root, RDFS.comment, Literal(f"{cq['id']}: {cq['question']}")))
    for note in profile["notes"]:
        graph.add((root, RDFS.comment, Literal("Modelling note: " + note)))
    for item in profile["unresolved_requirements"]:
        graph.add((root, RDFS.comment, Literal(f"UNRESOLVED {item['requirement_id']}: {item['reason']}")))
    selected = catalogue.expand(profile["selected_terms"])
    for name in selected:
        term = catalogue.terms[name]
        node = catalogue.node(name)
        prefix, local = term["declaration"].split(":")
        graph.add((node, RDF.type, URIRef(STANDARD[prefix] + local)))
        graph.add((node, RDFS.label, Literal(term["label"])))
        graph.add((node, RDFS.comment, Literal(term["definition"])))
        graph.add((root, RDFS.seeAlso, URIRef(term["source"])))
        for parent in term.get("parents", []):
            graph.add((node, RDFS.subClassOf, catalogue.node(parent)))
        for key, predicate in (("domain", RDFS.domain), ("range", RDFS.range)):
            if key in term:
                graph.add((node, predicate, catalogue.node(term[key])))
        if term.get("inverse") in selected:
            graph.add((node, OWL.inverseOf, catalogue.node(term["inverse"])))
    for use in profile["property_uses"]:
        shape, constraint = BNode(), BNode()
        graph.add((shape, RDF.type, SH.NodeShape))
        graph.add((shape, SH.targetClass, catalogue.node(use["on_class"])))
        graph.add((shape, SH.property, constraint))
        graph.add((constraint, SH.path, catalogue.node(use["property"])))
        alternatives = [(SH["class"], catalogue.node(c)) for c in use["value_classes"]]
        alternatives += [(SH.datatype, RDF.langString if dt == "langString" else URIRef(str(XSD) + dt)) for dt in use["datatypes"]]
        if len(alternatives) == 1:
            predicate, value = alternatives[0]
            graph.add((constraint, predicate, value))
        else:
            branches = []
            for predicate, value in alternatives:
                branch = BNode(); graph.add((branch, predicate, value)); branches.append(branch)
            head = BNode(); Collection(graph, head, branches); graph.add((constraint, SH["or"], head))
    # Defensive invariant: every named domain entity declaration came from the catalogue.
    permitted = {URIRef(t["iri"]) for t in catalogue.terms.values()}
    for kind in (OWL.Class, RDFS.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty, RDF.Property):
        if any(isinstance(s, URIRef) and s not in permitted for s in graph.subjects(RDF.type, kind)):
            raise ValueError("Unexpected locally minted class or property.")
    return graph
