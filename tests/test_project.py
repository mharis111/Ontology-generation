"""Offline contract tests. Simulated model output is never presented as LLM validation."""
import copy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rdflib import Graph, URIRef, BNode, RDF, RDFS, OWL, XSD
from rdflib.collection import Collection
from rdflib.compare import isomorphic
import providers
from ontology_engine import Config, load_project, build_messages, generate, run_from_files
from vocabulary import SH, validate_profile, build_schema, build_graph, merge_profiles, Catalogue, PropertyValueError
from requirements_io import scope_orsd, validate_orsd, read_json_file


def fixture():
    """A test-only valid response covering the supplied pilot requirements."""
    uses = [
        ("cito:cites", "schema:CreativeWork", ["schema:CreativeWork"], []),
        ("cito:isCitedBy", "schema:CreativeWork", ["schema:CreativeWork"], []),
        ("schema:author", "schema:CreativeWork", ["schema:Person", "schema:Organization"], []),
        ("dcat:dataset", "dcat:Catalog", ["dcat:Dataset"], []),
        ("dcterms:subject", "dcat:Dataset", ["skos:Concept"], []),
        ("dcat:keyword", "dcat:Dataset", [], ["string", "langString"]),
        ("skos:prefLabel", "skos:Concept", [], ["string", "langString"]),
    ]
    uses += [("dcterms:identifier", cls, [], ["string"]) for cls in
             ("schema:CreativeWork", "schema:Person", "schema:Organization", "dcat:Dataset")]
    records = [dict(property=p, on_class=c, value_classes=values, datatypes=types)
               for p, c, values, types in uses]
    selected = {"schema:ScholarlyArticle", "schema:Book", "schema:Report"}
    for item in records:
        selected.update([item["property"], item["on_class"], *item["value_classes"]])
    return dict(selected_terms=sorted(selected), property_uses=records, notes=[], unresolved_requirements=[])


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("inputs", "prompts", "vocabularies"):
            shutil.copytree(ROOT / name, self.root / name)
        shutil.copy(ROOT / "settings.json", self.root / "settings.json")
        self.settings = self.root / "settings.json"
        self.project = load_project(self.settings)
        self.cat = self.project["catalogue"]
        self.ids = {"CQ_001", "CQ_002", "CQ_003a", "CQ_003b", "FR_01"}

    def change_settings(self, change):
        data = json.loads(self.settings.read_text())
        change(data)
        self.settings.write_text(json.dumps(data))
        self.project = load_project(self.settings)

    def complete(self, messages, config):
        return json.dumps(fixture()), {"simulated": True}

    def run_model(self, fn=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return generate(self.project, completion_fn=fn or self.complete)

    def test_default_loads_all_four_cqs_and_schema(self):
        self.assertEqual(len(self.project["selected_cqs"]), 4)
        self.assertIn("schema:author", self.cat.terms)
        self.assertEqual(self.project["config"].mode, "joint")
        self.assertEqual(len(self.cat.terms), 26)

    def test_valid_profile_and_full_iri_canonicalization(self):
        data = fixture()
        data["selected_terms"] = [self.cat.terms[t]["iri"] for t in data["selected_terms"]]
        result = validate_profile(json.dumps(data), self.cat, self.ids)
        self.assertIn("schema:ScholarlyArticle", result["selected_terms"])

    def test_no_local_or_invented_terms(self):
        for term in ("lumis:Article", "Article", "schema:Publication", "https://example.org/lumis/Author", "IRI"):
            with self.subTest(term=term), self.assertRaisesRegex(ValueError, "Unapproved term"):
                data = fixture(); data["selected_terms"].append(term)
                validate_profile(data, self.cat, self.ids)

    def test_old_local_definition_arrays_rejected(self):
        data = fixture(); data["classes"] = [{"id": "Article"}]
        with self.assertRaisesRegex(ValueError, "Local class/property"):
            validate_profile(data, self.cat, self.ids)

    def test_undeclared_reference_rejected(self):
        data = fixture(); data["selected_terms"].remove("schema:Person")
        with self.assertRaisesRegex(ValueError, "selected_terms"):
            validate_profile(data, self.cat, self.ids)

    def test_class_property_confusion_rejected(self):
        data = fixture(); data["property_uses"][0]["property"] = "schema:Person"
        with self.assertRaisesRegex(ValueError, "not property"):
            validate_profile(data, self.cat, self.ids)

    def test_string_is_not_resource_class(self):
        data = fixture(); data["property_uses"][0]["value_classes"] = ["string"]
        with self.assertRaisesRegex(ValueError, "Unapproved term"):
            validate_profile(data, self.cat, self.ids)

    def test_resource_literal_direction_rejected(self):
        for index, values, datatypes in [(0, [], ["string"]), (5, ["schema:Person"], [])]:
            with self.subTest(index=index), self.assertRaises(ValueError):
                data = fixture()
                data["property_uses"][index].update(value_classes=values, datatypes=datatypes)
                validate_profile(data, self.cat, self.ids)

    def test_schema_partitions_properties_by_value_kind(self):
        branches = build_schema(self.cat)["properties"]["property_uses"]["items"]["anyOf"]
        seen = set()
        self.assertEqual(len(branches), 2)
        for branch in branches:
            fields = branch["properties"]
            names = fields["property"]["enum"]
            kinds = {self.cat.terms[name]["value_kind"] for name in names}
            self.assertEqual(len(kinds), 1)
            kind = kinds.pop()
            self.assertFalse(seen.intersection(names))
            seen.update(names)
            if kind == "literal":
                self.assertEqual(fields["value_classes"]["maxItems"], 0)
                self.assertEqual(fields["datatypes"]["minItems"], 1)
            else:
                self.assertEqual(fields["datatypes"]["maxItems"], 0)
                self.assertEqual(fields["value_classes"]["minItems"], 1)
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["required"]), {"property", "on_class", "value_classes", "datatypes"})
        self.assertEqual(seen, {name for name, term in self.cat.terms.items() if term["kind"] == "property"})

    def test_schema_handles_catalogue_with_only_literal_properties(self):
        data = copy.deepcopy(self.cat.data)
        data["terms"] = [t for t in data["terms"] if t["kind"] == "class" or t["value_kind"] == "literal"]
        branches = build_schema(Catalogue(data))["properties"]["property_uses"]["items"]["anyOf"]
        self.assertEqual(len(branches), 1)
        self.assertIn("dcterms:identifier", branches[0]["properties"]["property"]["enum"])

    def test_identifier_mismatch_has_exact_location_and_repair_shape(self):
        data = fixture()
        data["property_uses"][7].update(value_classes=["schema:Person"], datatypes=[])
        with self.assertRaises(PropertyValueError) as caught:
            validate_profile(data, self.cat, self.ids)
        error = caught.exception
        self.assertIn("property_uses[7]", str(error))
        hint = error.repair_hint()
        self.assertIn('"on_class": "schema:CreativeWork"', hint)
        self.assertIn('"value_classes": []', hint)
        self.assertIn('"datatypes": ["string"]', hint)
        # Validation provides a hint, never rewrites the caller's proposed model.
        self.assertEqual(data["property_uses"][7]["value_classes"], ["schema:Person"])

    def test_identifier_datatype_in_class_field_gets_literal_diagnostic(self):
        data = fixture()
        data["property_uses"][7].update(value_classes=["string"], datatypes=[])
        with self.assertRaisesRegex(PropertyValueError, "identifier uses literal values"):
            validate_profile(data, self.cat, self.ids)

    def test_identifier_regression_repaired_by_full_profile_response(self):
        bad = fixture()
        bad["property_uses"][7].update(value_classes=["schema:Person"], datatypes=[])
        calls = []
        def complete(messages, config):
            calls.append(messages)
            return json.dumps(bad if len(calls) == 1 else fixture()), {}
        result = self.run_model(complete)
        self.assertEqual(result["status"], "generated")
        self.assertEqual(len(calls), 2)
        self.assertIn("COMPLETE corrected profile", calls[1][-1]["content"])
        self.assertIn('"value_classes": []', calls[1][-1]["content"])
        self.assertIn('"datatypes": ["string"]', calls[1][-1]["content"])
        graph = Graph().parse(result["output_file"], format="xml")
        for shape in graph.subjects(SH.path, self.cat.node("dcterms:identifier")):
            self.assertEqual(graph.value(shape, SH.datatype), XSD.string)
            self.assertFalse(list(graph.objects(shape, SH["class"])))
        self.assertEqual(len(list((self.root / "outputs").iterdir())), 1)

    def test_repeated_identifier_mismatch_fails_without_auto_conversion(self):
        bad = fixture()
        bad["property_uses"][7].update(value_classes=["schema:Person"], datatypes=[])
        result = self.run_model(lambda m, c: (json.dumps(bad), {}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertIn("property_uses[7]", result["results"][0]["error"])
        self.assertFalse((self.root / "outputs").exists())

    def test_duplicate_usage_rejected(self):
        data = fixture(); data["property_uses"].append(copy.deepcopy(data["property_uses"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate property/class"):
            validate_profile(data, self.cat, self.ids)

    def test_native_dcat_domain_and_range_respected(self):
        for key, value in [("on_class", "schema:CreativeWork"), ("value_classes", ["schema:Person"])]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "compatible"):
                data = fixture(); data["property_uses"][3][key] = value
                validate_profile(data, self.cat, self.ids)

    def test_model_duplicate_keys_and_nan_rejected(self):
        for raw in ('{"selected_terms":[],"selected_terms":[]}', '{"value":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_profile(raw, self.cat, self.ids)

    def test_unknown_unresolved_requirement_rejected(self):
        data = fixture(); data["unresolved_requirements"] = [{"requirement_id": "CQ_999", "reason": "missing"}]
        with self.assertRaisesRegex(ValueError, "known active"):
            validate_profile(data, self.cat, self.ids)

    def test_graph_contains_only_approved_named_vocabulary(self):
        result = self.run_model()
        graph = Graph().parse(result["output_file"], format="xml")
        for kind in (OWL.Class, RDFS.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty, RDF.Property):
            self.assertTrue(all(str(s) in self.cat.by_iri for s in graph.subjects(RDF.type, kind)))
        self.assertFalse(any("lumis" in str(n).lower() for triple in graph for n in triple if isinstance(n, URIRef)))
        self.assertEqual(len(set(graph.subjects(RDF.type, OWL.Ontology))), 1)

    def test_schema_native_hierarchy_preserved(self):
        graph = build_graph(fixture(), self.cat, self.project["ontology_iri"], {}, [], {})
        for child, parent in [("ScholarlyArticle", "Article"), ("Report", "Article"), ("Article", "CreativeWork")]:
            self.assertIn((self.cat.node("schema:" + child), RDFS.subClassOf, self.cat.node("schema:" + parent)), graph)

    def test_author_alternatives_use_shacl_or_not_global_intersection(self):
        graph = build_graph(fixture(), self.cat, self.project["ontology_iri"], {}, [], {})
        prop = self.cat.node("schema:author")
        self.assertFalse(list(graph.objects(prop, RDFS.domain)))
        self.assertFalse(list(graph.objects(prop, RDFS.range)))
        constraints = list(graph.subjects(SH.path, prop))
        self.assertEqual(len(constraints), 1)
        branches = Collection(graph, graph.value(constraints[0], SH["or"]))
        self.assertEqual({graph.value(b, SH["class"]) for b in branches},
                         {self.cat.node("schema:Person"), self.cat.node("schema:Organization")})
        self.assertTrue(all(isinstance(s, BNode) for s in graph.subjects(RDF.type, SH.NodeShape)))

    def test_inverse_axiom_only_for_selected_pair(self):
        data = fixture()
        graph = build_graph(data, self.cat, self.project["ontology_iri"], {}, [], {})
        self.assertIn((self.cat.node("cito:cites"), OWL.inverseOf, self.cat.node("cito:isCitedBy")), graph)
        data["selected_terms"].remove("cito:isCitedBy")
        data["property_uses"].pop(1)
        graph = build_graph(data, self.cat, self.project["ontology_iri"], {}, [], {})
        self.assertFalse(list(graph.triples((None, OWL.inverseOf, None))))

    def test_all_modes_write_exactly_one_file(self):
        for mode, calls in [("joint", 1), ("independent", 4), ("incremental", 4)]:
            with self.subTest(mode=mode):
                self.project["config"].mode = mode
                with patch.object(self, "complete", wraps=self.complete) as complete:
                    result = self.run_model(complete)
                self.assertEqual(complete.call_count, calls)
                self.assertEqual(result["status"], "generated")
                self.assertEqual([p.name for p in (self.root / "outputs").iterdir()], ["ontology.owl"])

    def test_turtle_optional_and_rerun_overwrites_same_file(self):
        self.change_settings(lambda s: s["output"].update(file="outputs/ontology.ttl"))
        first = self.run_model(); second = self.run_model()
        self.assertEqual(first["output_file"], second["output_file"])
        self.assertGreater(len(Graph().parse(second["output_file"], format="turtle")), 0)
        self.assertEqual(len(list((self.root / "outputs").iterdir())), 1)

    def test_failure_writes_nothing_and_preserves_previous_output(self):
        def invalid(messages, config):
            return '{"turtle":"..."}', {}
        result = self.run_model(invalid)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "outputs").exists())
        accepted = self.run_model(); original = Path(accepted["output_file"]).read_bytes()
        self.run_model(invalid)
        self.assertEqual(Path(accepted["output_file"]).read_bytes(), original)
        self.assertEqual(len(list((self.root / "outputs").iterdir())), 1)

    def test_no_overwrite_stops_before_provider(self):
        self.run_model(); self.project["overwrite"] = False
        with patch.object(self, "complete") as complete, self.assertRaisesRegex(ValueError, "already exists"):
            self.run_model(complete)
        complete.assert_not_called()

    def test_bounded_repair_preserves_prompt_and_no_think(self):
        captured = []
        def complete(messages, config):
            captured.append(messages)
            return ('{"turtle":"..."}' if len(captured) == 1 else json.dumps(fixture())), {}
        self.assertEqual(self.run_model(complete)["status"], "generated")
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1][:2])
        self.assertTrue(captured[1][-1]["content"].endswith("/no_think"))

    def test_provider_failure_not_treated_as_repairable_syntax(self):
        def complete(messages, config):
            raise providers.ProviderError("truncated", raw_text="partial", metadata={"done_reason": "length"})
        result = self.run_model(complete)
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["raw"], "partial")
        self.assertIsNone(result["output_file"])

    def test_unresolved_requirements_recorded_as_needs_review(self):
        data = fixture(); data["unresolved_requirements"] = [{"requirement_id": "CQ_002", "reason": "Mapping requires review"}]
        result = self.run_model(lambda m, c: (json.dumps(data), {}))
        self.assertEqual(result["status"], "needs_review")
        graph = Graph().parse(result["output_file"])
        self.assertTrue(any("UNRESOLVED CQ_002" in str(v) for v in graph.objects(None, RDFS.comment)))
        self.assertEqual(result["cq_coverage"], "not_checked")

    def test_incremental_failure_blocks_remaining_cqs(self):
        self.project["config"].mode = "incremental"
        result = self.run_model(lambda m, c: ("{}", {}))
        self.assertEqual([r["status"] for r in result["results"]], ["failed", "blocked", "blocked", "blocked"])
        self.assertFalse((self.root / "outputs").exists())

    def test_incremental_cannot_silently_change_prior_constraints(self):
        first, second = fixture(), fixture()
        second["property_uses"][2]["value_classes"] = ["schema:Person"]
        with self.assertRaisesRegex(ValueError, "changed its value constraints"):
            merge_profiles(first, second, allow_union=False)

    def test_independent_merges_alternative_values_as_union(self):
        first, second = fixture(), fixture()
        first["property_uses"][2]["value_classes"] = ["schema:Person"]
        second["property_uses"][2]["value_classes"] = ["schema:Organization"]
        result = merge_profiles(first, second)
        use = next(u for u in result["property_uses"] if u["property"] == "schema:author")
        self.assertEqual(use["value_classes"], ["schema:Organization", "schema:Person"])

    def test_inputs_reread_at_runtime_with_exact_hashes(self):
        changes = [("cqs", "runtime CQ change"), ("orsd", "runtime ORSD change"), ("prompt", "runtime prompt change")]
        for role, value in changes:
            path = self.project["paths"][role]
            if role == "cqs":
                data = json.loads(path.read_text()); data[0]["notes"] = value; path.write_text(json.dumps(data))
            elif role == "orsd":
                data = json.loads(path.read_text()); data["additional_context"] = value; path.write_text(json.dumps(data))
            else:
                path.write_text(path.read_text() + "\n" + value)
        project = load_project(self.settings)
        prompt = json.dumps(build_messages(project))
        for role, value in changes:
            self.assertIn(value, prompt)
            self.assertEqual(project["input_hashes"][role], hashlib.sha256(project["paths"][role].read_bytes()).hexdigest())

    def test_relative_paths_resolve_from_settings_not_cwd(self):
        cwd = Path.cwd()
        try:
            os.chdir(self.root / "inputs")
            self.assertEqual(load_project(self.settings)["output_file"], self.root / "outputs/ontology.owl")
        finally:
            os.chdir(cwd)

    def test_selection_and_scoped_orsd(self):
        self.change_settings(lambda s: s.update(cq_ids=["CQ_001", "CQ_002", "CQ_003a"]))
        messages = json.dumps(build_messages(self.project))
        self.assertNotIn('"id": "FR_04"', messages.replace('\\"', '"'))
        self.assertEqual(len(self.project["selected_cqs"]), 3)

    def test_invalid_input_reference_rejected_before_generation(self):
        data = copy.deepcopy(self.project["orsd"])
        data["functional_requirements"][0]["applies_to"] = ["CQ_MISSING"]
        with self.assertRaisesRegex(ValueError, "unknown CQ"):
            validate_orsd(data, self.project["cqs"])

    def test_table_requirements_validated(self):
        data = copy.deepcopy(self.project["orsd"])
        data["functional_requirements"][3]["rows"][0].pop("meaning")
        with self.assertRaisesRegex(ValueError, "specified columns"):
            validate_orsd(data, self.project["cqs"])

    def test_invalid_json_file_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                read_json_file("test.json", raw)

    def test_catalogue_cannot_hijack_standard_or_local_prefix(self):
        for prefix in ("owl", "lumis"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                data = copy.deepcopy(self.cat.data); data["prefixes"][prefix] = "https://example.org/local/"
                Catalogue(data)

    def test_output_input_collision_and_external_header_rejected(self):
        for output in ({"file": "inputs/cqs.json"}, {"ontology_iri": "https://schema.org/myOntology"}, {"ontology_iri": "https://example.org/lumis/"}):
            data = json.loads(self.settings.read_text()); data["output"].update(output)
            self.settings.write_text(json.dumps(data))
            with self.subTest(output=output), self.assertRaises(ValueError):
                load_project(self.settings)

    def test_cli_preview_does_not_contact_provider_or_write_output(self):
        result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--config", str(self.settings), "--preview"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema:author", result.stdout)
        self.assertFalse((self.root / "outputs").exists())

    def test_ollama_payload_schema_and_thinking_controls(self):
        with patch.object(providers, "http_json", return_value={"done": True, "done_reason": "stop", "message": {"content": json.dumps(fixture())}}) as request:
            raw, metadata = providers.generate_text(build_messages(self.project), self.project["config"], build_schema(self.cat))
        payload = request.call_args.args[1]
        self.assertIs(payload["think"], False)
        self.assertIsInstance(payload["format"], dict)
        self.assertEqual(payload["options"]["num_ctx"], 16384)
        self.assertFalse(metadata["thinking_present"])
        validate_profile(raw, self.cat, self.ids)

    def test_ollama_incomplete_truncated_and_empty_outputs_rejected(self):
        for response in [
            {"done": False, "message": {"content": "partial"}},
            {"done": True, "done_reason": "length", "message": {"content": "partial"}},
            {"done": True, "done_reason": "stop", "message": {"content": "", "thinking": "analysis"}},
        ]:
            with self.subTest(response=response), patch.object(providers, "http_json", return_value=response), self.assertRaises(providers.ProviderError):
                providers.generate_text([], self.project["config"], build_schema(self.cat))

    def test_openai_key_is_header_only_and_incomplete_response_rejected(self):
        config = Config(provider="openai", model="chosen-model")
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(fixture())}}]}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}), patch.object(providers, "http_json", return_value=response) as request:
            raw, meta = providers.generate_text([], config, build_schema(self.cat))
            self.assertNotIn("test-secret", json.dumps(request.call_args.args))
            self.assertNotIn("test-secret", json.dumps(meta))
            self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")
            response["choices"][0]["finish_reason"] = "length"
            with self.assertRaises(providers.ProviderError):
                providers.generate_text([], config, build_schema(self.cat))

    def test_preflight_rejects_missing_ollama_model_and_openai_key(self):
        with patch.object(providers, "http_json", return_value={"models": []}), self.assertRaisesRegex(providers.ProviderError, "not installed"):
            providers.preflight(Config())
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), self.assertRaisesRegex(providers.ProviderError, "OPENAI_API_KEY"):
            providers.preflight(Config(provider="openai"))


if __name__ == "__main__":
    unittest.main()
