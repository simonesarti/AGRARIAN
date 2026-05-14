from pathlib import Path

import pytest

from utils import (
    parse_config_file,
    read_yaml_config,
    is_valid_pt_file,
    is_valid_yaml_conf,
    is_valid_checkpoint,
    is_valid_tracker,
    is_valid_youtube_link,
    is_valid_image,
    is_valid_images_dir,
    is_valid_video,
    is_valid_videos_dir,
    ALLOWED_IMAGE_FORMATS,
    ALLOWED_VIDEO_FORMATS,
    BASE_CHECKPOINTS_DETECT,
    BASE_CHECKPOINTS_SEGMENT,
    BASE_TRACKERS,
)


# ---------------------------------------------------------------------------
# Tests for parse_config_file
# ---------------------------------------------------------------------------

def test_parse_config_file(monkeypatch):
    """
    Simulate command-line arguments so that the --config parameter is provided.
    """
    test_config = "config.yaml"
    monkeypatch.setattr("sys.argv", ["prog", "--config", test_config])
    result = parse_config_file()
    assert result == test_config


# ---------------------------------------------------------------------------
# Tests for read_yaml_config
# ---------------------------------------------------------------------------

def test_read_yaml_config_valid(tmp_path):
    """
    Create a temporary valid YAML file and test that it is correctly read.
    """
    content = "key: value\nlist:\n  - 1\n  - 2"
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(content)
    config = read_yaml_config(str(yaml_file))
    assert isinstance(config, dict)
    assert config.get("key") == "value"
    assert config.get("list") == [1, 2]


def test_read_yaml_config_file_not_found(tmp_path, capsys):
    """
    If the YAML file is not found, the function should print an error and exit.
    """
    non_existent = tmp_path / "nonexistent.yaml"
    with pytest.raises(SystemExit):
        read_yaml_config(str(non_existent))
    captured = capsys.readouterr().out
    assert f"YAML configuration file '{non_existent}' not found" in captured


def test_read_yaml_config_invalid_yaml(tmp_path, capsys):
    """
    If the YAML file contains invalid YAML, the function should print an error and exit.
    """
    yaml_file = tmp_path / "invalid.yaml"
    # Write content that is not valid YAML.
    yaml_file.write_text("key: [unclosed")
    with pytest.raises(SystemExit):
        read_yaml_config(str(yaml_file))
    captured = capsys.readouterr().out
    assert "Error parsing YAML file:" in captured


# ---------------------------------------------------------------------------
# Tests for is_valid_pt_file and is_valid_yaml_conf
# ---------------------------------------------------------------------------

def test_is_valid_pt_file(tmp_path):
    valid_file = tmp_path / "model.pt"
    valid_file.write_text("dummy")
    invalid_file = tmp_path / "model.txt"
    invalid_file.write_text("dummy")
    non_existing = tmp_path / "nonexistent.pt"

    assert is_valid_pt_file(valid_file)
    assert not is_valid_pt_file(invalid_file)
    assert not is_valid_pt_file(non_existing)


def test_is_valid_yaml_conf(tmp_path):
    valid_file = tmp_path / "config.yaml"
    valid_file.write_text("dummy")
    invalid_file = tmp_path / "config.txt"
    invalid_file.write_text("dummy")
    non_existing = tmp_path / "nonexistent.yaml"

    assert is_valid_yaml_conf(valid_file)
    assert not is_valid_yaml_conf(invalid_file)
    assert not is_valid_yaml_conf(non_existing)


# ---------------------------------------------------------------------------
# Tests for is_valid_checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base_cp", BASE_CHECKPOINTS_DETECT)
def test_is_valid_checkpoint_detect_base(base_cp: str):
    """
    For task 'detect', if the checkpoint string is in BASE_CHECKPOINTS_DETECT,
    the function returns True even if the file does not exist.
    """
    cp = Path(base_cp)
    assert cp.as_posix() in BASE_CHECKPOINTS_DETECT
    assert is_valid_checkpoint(cp, "detect")


@pytest.mark.parametrize("base_cp", BASE_CHECKPOINTS_SEGMENT)
def test_is_valid_checkpoint_segment_base(base_cp: str):
    """
    For task 'segment', if the checkpoint string is in BASE_CHECKPOINTS_SEGMENT,
    the function returns True even if the file does not exist.
    """
    cp = Path(base_cp)
    assert cp.as_posix() in BASE_CHECKPOINTS_SEGMENT
    assert is_valid_checkpoint(cp, "segment")


def test_is_valid_checkpoint_with_file(tmp_path):
    """
    Create a temporary .pt file that is not in the base lists.
    """
    cp = tmp_path / "custom.pt"
    cp.write_text("dummy")
    assert is_valid_checkpoint(cp, "detect")
    assert is_valid_checkpoint(cp, "segment")


def test_is_valid_checkpoint_invalid_task(tmp_path):
    """
    For an unsupported task, is_valid_checkpoint should raise NotImplementedError.
    """
    cp = tmp_path / "custom.pt"
    cp.write_text("dummy")
    with pytest.raises(NotImplementedError):
        is_valid_checkpoint(cp, "unknown")


# ---------------------------------------------------------------------------
# Tests for is_valid_tracker
# ---------------------------------------------------------------------------

def test_is_valid_tracker_with_yaml(tmp_path):
    """
    When a valid YAML file is provided, is_valid_tracker should return True.
    """
    tracker_yaml = tmp_path / "tracker.yaml"
    tracker_yaml.write_text("dummy")
    assert is_valid_tracker(tracker_yaml)


@pytest.mark.parametrize("base_tracker", BASE_TRACKERS)
def test_is_valid_tracker_with_base_name(base_tracker: str):
    """
    Even if a file does not exist, if its string is in BASE_TRACKERS,
    is_valid_tracker should return True.
    """
    tracker_name = Path(base_tracker)
    assert tracker_name.as_posix() in BASE_TRACKERS
    assert is_valid_tracker(tracker_name)


def test_is_valid_tracker_invalid(tmp_path):
    """
    When a file with a wrong extension is provided, is_valid_tracker should return False.
    """
    not_tracker = tmp_path / "not_tracker.txt"
    not_tracker.write_text("dummy")
    assert not is_valid_tracker(not_tracker)


# ---------------------------------------------------------------------------
# Tests for is_valid_youtube_link
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url, expected", [
    # Valid YouTube URLs
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", True),
    ("https://youtu.be/dQw4w9WgXcQ", True),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", True),
    ("https://www.youtube.com/v/dQw4w9WgXcQ", True),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=youtu.be", True),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ", True),
    # Invalid URLs
    ("https://www.example.com/watch?v=dQw4w9WgXcQ", False),  # Wrong domain
    ("https://www.youtube.com/", False),  # No video ID
    ("https://youtu.be/", False),  # No video ID
    ("https://www.youtube.com/watch?v=", False),  # Empty video ID
    ("https://www.youtube.com/watch", False),  # Missing "v=" parameter
    ("https://www.youtube.com/embed/", False),  # No video ID
    ("dQw4w9WgXcQ", False),  # Just a video ID, not a URL
    ("", False),  # Empty string
    (None, False),  # None value
])
def test_is_valid_youtube_link(url, expected):
    assert is_valid_youtube_link(url) == expected


# -------------------------------------------------------
# Tests for is_valid_image
# -------------------------------------------------------

def test_is_valid_image_with_valid_extension(tmp_path: Path):
    # Create a temporary file with a valid image extension.
    valid_file = tmp_path / "test.jpg"
    valid_file.write_text("dummy content")
    assert is_valid_image(valid_file) is True


@pytest.mark.parametrize("ext", ALLOWED_IMAGE_FORMATS)
def test_is_valid_image_all_allowed_extensions(tmp_path: Path, ext: str):
    # Test every allowed extension (using uppercase to verify case-insensitivity)
    valid_file = tmp_path / f"image{ext.upper()}"
    valid_file.write_text("dummy")
    assert is_valid_image(valid_file) is True


def test_is_valid_image_with_invalid_extension(tmp_path: Path):
    # Create a file with an extension not in the allowed list.
    invalid_file = tmp_path / "test.txt"
    invalid_file.write_text("dummy content")
    assert is_valid_image(invalid_file) is False


def test_is_valid_image_nonexistent(tmp_path: Path):
    # Reference a file that does not exist.
    nonexistent = tmp_path / "nonexistent.jpg"
    assert is_valid_image(nonexistent) is False


def test_is_valid_image_when_given_directory(tmp_path: Path):
    # Passing a directory instead of a file should return False.
    directory = tmp_path / "subdir"
    directory.mkdir()
    assert is_valid_image(directory) is False


# -------------------------------------------------------
# Tests for is_valid_video
# -------------------------------------------------------

def test_is_valid_video_with_valid_extension(tmp_path: Path):
    valid_file = tmp_path / "video.mp4"
    valid_file.write_text("dummy content")
    assert is_valid_video(valid_file) is True


@pytest.mark.parametrize("ext", ALLOWED_VIDEO_FORMATS)
def test_is_valid_video_all_allowed_extensions(tmp_path: Path, ext: str):
    valid_file = tmp_path / f"video{ext.upper()}"
    valid_file.write_text("dummy")
    assert is_valid_video(valid_file) is True


def test_is_valid_video_with_invalid_extension(tmp_path: Path):
    invalid_file = tmp_path / "video.txt"
    invalid_file.write_text("dummy content")
    assert is_valid_video(invalid_file) is False


def test_is_valid_video_nonexistent(tmp_path: Path):
    nonexistent = tmp_path / "nonexistent.mp4"
    assert is_valid_video(nonexistent) is False


def test_is_valid_video_when_given_directory(tmp_path: Path):
    directory = tmp_path / "subdir"
    directory.mkdir()
    assert is_valid_video(directory) is False


# -------------------------------------------------------
# Tests for is_valid_images_dir
# -------------------------------------------------------

def test_is_valid_images_dir_all_valid(tmp_path: Path):
    # Create a directory with files that all have allowed image extensions.
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    for ext in ALLOWED_IMAGE_FORMATS:
        f = images_dir / f"image{ext}"
        f.write_text("dummy")
    assert is_valid_images_dir(images_dir) is True


def test_is_valid_images_dir_with_invalid_file(tmp_path: Path):
    # Create a directory with one valid image and one invalid file.
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    # Valid image file.
    (images_dir / "image.jpg").write_text("dummy")
    # Invalid file.
    (images_dir / "doc.txt").write_text("dummy")
    assert is_valid_images_dir(images_dir) is False


def test_is_valid_images_dir_empty(tmp_path: Path):
    # An empty directory should return True (since it doesn't contain any invalid files).
    empty_dir = tmp_path / "empty_images"
    empty_dir.mkdir()
    assert is_valid_images_dir(empty_dir) is True


def test_is_valid_images_dir_when_given_file(tmp_path: Path):
    # Passing a file instead of a directory should return False.
    file_path = tmp_path / "image.jpg"
    file_path.write_text("dummy")
    assert is_valid_images_dir(file_path) is False


# -------------------------------------------------------
# Tests for is_valid_videos_dir
# -------------------------------------------------------

def test_is_valid_videos_dir_all_valid(tmp_path: Path):
    # Create a directory with files that all have allowed video extensions.
    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    for ext in ALLOWED_VIDEO_FORMATS:
        f = videos_dir / f"video{ext}"
        f.write_text("dummy")
    assert is_valid_videos_dir(videos_dir) is True


def test_is_valid_videos_dir_with_invalid_file(tmp_path: Path):
    # Create a directory with one valid video file and one invalid file.
    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    (videos_dir / "video.mp4").write_text("dummy")
    (videos_dir / "doc.txt").write_text("dummy")
    assert is_valid_videos_dir(videos_dir) is False


def test_is_valid_videos_dir_empty(tmp_path: Path):
    empty_dir = tmp_path / "empty_videos"
    empty_dir.mkdir()
    assert is_valid_videos_dir(empty_dir) is True


def test_is_valid_videos_dir_when_given_file(tmp_path: Path):
    file_path = tmp_path / "video.mp4"
    file_path.write_text("dummy")
    assert is_valid_videos_dir(file_path) is False
