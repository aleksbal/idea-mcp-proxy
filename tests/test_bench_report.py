import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("bench_report", Path(__file__).parent.parent / "bench" / "report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def test_extract_answer_takes_last_json_block():
    text = 'first ```json\n{"callers": ["A.x"]}\n``` then final:\n```json\n{"callers": ["B.y"]}\n```'
    assert report.extract_answer(text) == {"callers": ["B.y"]}
    assert report.extract_answer("no block") is None


def test_normalize_tolerates_packages_params_and_hash():
    assert report.normalize("org.apache.commons.lang3.Strings#endsWithAny(CharSequence, CharSequence...)") == "strings.endswithany"
    assert report.normalize("Strings.endsWithAny") == "strings.endswithany"


def test_set_scoring_counts_traps_as_extra():
    task = {"scoring": "set", "answer_key": "callers", "expected": ["Strings.containsAny", "Validate.getMessage"]}
    sc = report.score(task, {"callers": ["Strings.containsAny", "Validate.getMessage", "StrMatcher.charSetMatcher"]})
    assert not sc["exact"] and sc["recall"] == 1.0 and sc["precision"] == 2 / 3
    assert sc["extra"] == ["strmatcher.charsetmatcher"]


def test_empty_expected_set_is_correct_only_when_answer_empty():
    task = {"scoring": "multi_set", "expected_sets": {"production_callers": [], "test_classes": ["ObjectUtilsTest"]}}
    assert report.score(task, {"production_callers": [], "test_classes": ["ObjectUtilsTest"]})["exact"]
    assert not report.score(task, {"production_callers": ["Range.min"], "test_classes": ["ObjectUtilsTest"]})["exact"]
    assert not report.score(task, None)["exact"]


def test_alternative_spellings_and_class_normalization():
    task = {"scoring": "set", "answer_key": "callers",
            "expected": ["EventBindingInvocationHandler.invoke|EventUtils.invoke", "MethodUtils.invokeMethod"]}
    assert report.score(task, {"callers": ["EventUtils.invoke", "MethodUtils.invokeMethod"]})["exact"]
    assert report.score(task, {"callers": ["EventUtils.EventBindingInvocationHandler.invoke", "MethodUtils.invokeMethod"]})["exact"]
    classes = {"scoring": "set", "answer_key": "classes", "normalize": "class",
               "expected": ["JsonToStringStyle", "AnnotationUtils"]}
    sc = report.score(classes, {"classes": ["ToStringStyle.JsonToStringStyle", "AnnotationUtils (anonymous ToStringStyle)"]})
    assert sc["exact"], sc


def test_file_names_normalize_to_their_class():
    assert report.normalize("ArchUtils.java", "class") == "archutils"
    assert report.normalize("src/main/java/org/x/ArchUtils.java", "class") == "archutils"
