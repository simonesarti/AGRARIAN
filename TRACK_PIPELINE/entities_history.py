import numpy as np
import json

class EntitiesTrackHistory:
    def __init__(self):
        self.history = {}  # {id: {"x": [], "y": [], "vis": [], "anom": []}}
        self.entity_order = []
        self.current_timestep = 0

    def update(self, ids, positions):
        """
        ids: List of N entity IDs
        positions: numpy array of shape (N, 2)
        """
        active_ids = set(ids)

        # 1. Handle existing entities or new ones
        for i, ent_id in enumerate(ids):
            if ent_id not in self.history:
                # New entity: Backfill with zeros for all previous timesteps
                self.entity_order.append(ent_id)
                self.history[ent_id] = {
                    "x": [0.0] * self.current_timestep,
                    "y": [0.0] * self.current_timestep,
                    "vis": [0] * self.current_timestep,
                    "anom": [0] * self.current_timestep,
                }
            
            # Add current data for visible entity
            self.history[ent_id]["x"].append(float(positions[i][0]))
            self.history[ent_id]["y"].append(float(positions[i][1]))
            self.history[ent_id]["vis"].append(1)
            self.history[ent_id]["anom"].append(0)

        # 2. Handle entities that are NOT visible this frame
        for ent_id in self.entity_order:
            if ent_id not in active_ids:
                # Replicate last known position, set visibility to 0
                last_x = self.history[ent_id]["x"][-1] if self.history[ent_id]["x"] else 0.0
                last_y = self.history[ent_id]["y"][-1] if self.history[ent_id]["y"] else 0.0
                
                self.history[ent_id]["x"].append(last_x)
                self.history[ent_id]["y"].append(last_y)
                self.history[ent_id]["vis"].append(0)
                self.history[ent_id]["anom"].append(0)

        self.current_timestep += 1

    def dump_json(self, save_path):
        """Dumps dictionary of entities and their 4 history lists."""
        with open(save_path, 'w') as f:
            json.dump(self.history, f, indent=4)

    def dump_metadata_json(self, save_path):
        """Dumps metadata including ID order and feature names."""
        metadata = {
            "entity_ids": self.entity_order,
            "features": [
                "normalized_screen_position_x", 
                "normalized_screen_position_y", 
                "camera_visible", 
                "anomaly_type",
            ]
        }
        with open(save_path, 'w') as f:
            json.dump(metadata, f, indent=4)

    def dump_npy(self, save_path):
        """Dumps a (N, T, 4) numpy array."""
        num_entities = len(self.entity_order)
        if num_entities == 0:
            np.save(save_path, np.array([]))
            return

        # Initialize array (N, T, 4)
        data_array = np.zeros((num_entities, self.current_timestep, 4), dtype=np.float32)

        for i, ent_id in enumerate(self.entity_order):
            h = self.history[ent_id]
            data_array[i, :, 0] = h["x"]
            data_array[i, :, 1] = h["y"]
            data_array[i, :, 2] = h["vis"]
            data_array[i, :, 3] = h["anom"]

        np.save(save_path, data_array)