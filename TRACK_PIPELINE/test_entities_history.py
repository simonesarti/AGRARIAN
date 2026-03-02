import pytest
import numpy as np
import json
from TRACK_PIPELINE.entities_history import EntitiesTrackHistory

class TestEntitiesTrackHistory:
    
    @pytest.fixture
    def tracker(self):
        return EntitiesTrackHistory()

    def test_initial_update(self, tracker):
        """Verify basic tracking of a single entity."""
        ids = [10]
        pos = np.array([[0.5, 0.5]])
        tracker.update(ids, pos)
        
        assert tracker.current_timestep == 1
        assert tracker.history[10]["vis"] == [1]
        assert tracker.history[10]["x"] == [0.5]
        assert tracker.history[10]["y"] == [0.5]
        assert tracker.history[10]["anom"] == [0]

    def test_backfilling_new_entities(self, tracker):
        """Verify that an entity appearing at T=2 has T=0,1 padded with 0s."""
        # Step 0
        tracker.update([1], np.array([[0.1, 0.1]]))
        # Step 1
        tracker.update([1], np.array([[0.2, 0.2]]))
        # Step 2: Entity 5 appears for the first time
        tracker.update([1, 5], np.array([[0.3, 0.3], [0.9, 0.7]]))

        history_5 = tracker.history[5]
        assert len(history_5["vis"]) == 3
        assert history_5["vis"] == [0, 0, 1]  # Invisible for first two frames
        assert history_5["x"] == [0.0, 0.0, 0.9]       # Backfilled with 0
        assert history_5["y"] == [0.0, 0.0, 0.7]       # Backfilled with 0
        assert history_5["anom"] == [0, 0, 0]       # Correct current pos

        history_1 = tracker.history[1]
        assert len(history_1["vis"]) == 3
        assert history_1["vis"] == [1, 1, 1]  # Invisible for first two frames
        assert history_1["x"] == [0.1, 0.2, 0.3]       # Backfilled with 0
        assert history_1["y"] == [0.1, 0.2, 0.3]       # Backfilled with 0
        assert history_1["anom"] == [0, 0, 0]       # Correct current pos

    def test_visibility_persistence(self, tracker):
        """Verify last known position is kept when an entity vanishes."""
        tracker.update([1], np.array([[0.1, 0.1]])) # T=0: Visible
        tracker.update([], np.empty((0, 2)))        # T=1: Vanished
        tracker.update([], np.empty((0, 2)))        # T=2: Vanished
        tracker.update([1], np.array([[0.2, 0.4]]))        # T=3: Reappears
        
        assert tracker.history[1]["vis"] == [1, 0, 0, 1]
        assert tracker.history[1]["x"] == [0.1, 0.1, 0.1, 0.2] # Replicated last known X
        assert tracker.history[1]["y"] == [0.1, 0.1, 0.1, 0.4] # Replicated last known Y
        assert tracker.history[1]["anom"] == [0,0,0,0] # Replicated last known Y

    def test_npy_and_metadata_alignment(self, tracker, tmp_path):
        """Verify the NPY array matches the metadata order and dimensions."""
        # Setup history: ID 5 then ID 2
        tracker.update([5], np.array([[0.1, 0.1]]))
        tracker.update([2], np.array([[0.2, 0.2]])) # 5 is now invisible
        tracker.update([5], np.array([[0.3, 0.3]])) # 5 is now invisible
        tracker.update([2,5], np.array([[0.4, 0.4],[0.5,0.5]])) # 5 is now invisible

        npy_path = tmp_path / "data.npy"
        meta_path = tmp_path / "meta.json"
        
        tracker.dump_npy(npy_path)
        tracker.dump_metadata_json(meta_path)
        
        data = np.load(npy_path)
        with open(meta_path, 'r') as f:
            meta = json.load(f)

        # Shape should be (N=2, T=4, Features=4)
        assert data.shape == (2, 4, 4)
        assert meta["entity_ids"] == [5, 2]
        assert meta["features"] == [
                "normalized_screen_position_x", 
                "normalized_screen_position_y", 
                "camera_visible", 
                "anomaly_type",
            ]
        
        expected_data = np.array(
            [
                [
                    [0.1, 0.1, 1.0, 0.0],   # T=0, id=5, visible at (0.1, 0.1)
                    [0.1, 0.1, 0.0, 0.0],   # T=1, id=5, invisible -> replicate (0.1, 0.1)
                    [0.3, 0.3, 1.0, 0.0],   # T=2, id=5, visible at (0.3, 0.3)
                    [0.5, 0.5, 1.0, 0.0],   # T=3, id=5, visible at (0.5, 0.5)
                ],
                [
                    [0.0, 0.0, 0.0, 0.0],   # T=0, id=1, backfilled
                    [0.2, 0.2, 1.0, 0.0],   # T=2, id=1, visible at (0.2, 0.2)
                    [0.2, 0.2, 0.0, 0.0],   # T=2, id=1, invisible -> replicate (0.2, 0.2)
                    [0.4, 0.4, 1.0, 0.0],   # T=3, id=1, visible at (0.4, 0.4)
                ],
            ]
        )

        print(data)
        print(expected_data)

        assert np.allclose(data, expected_data, atol=1e-8)

    def test_empty_updates(self, tracker):
        """Ensure class handles frames with no detections gracefully."""
        tracker.update([], np.empty((0, 2)))
        assert tracker.current_timestep == 1
        assert len(tracker.entity_order) == 0
        assert len(tracker.history) == 0