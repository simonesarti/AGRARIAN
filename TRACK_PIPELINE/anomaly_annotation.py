import numpy as np
import json
import pandas as pd
import os
from argparse import ArgumentParser

def update_anomalies_grouped(npy_path, json_path, tsv_path, output_path=None):
    # 1. Load metadata and create ID mapping
    if not os.path.exists(json_path):
        print(f"Error: Metadata file {json_path} not found.")
        return
    
    with open(json_path, 'r') as f:
        metadata = json.load(f)
    
    entity_ids = [str(eid) for eid in metadata.get('entity_ids', [])]
    id_to_idx = {eid: i for i, eid in enumerate(entity_ids)}

    # 2. Load the numpy array
    data = np.load(npy_path)
    N, T, C = data.shape
    print(f"Array: {N} entities, {T} steps. Modifying column index 3.")

    # 3. Process the TSV manually (since row lengths vary)
    success_count = 0
    skipped_rows = 0
    line_num = 0
    
    with open(tsv_path, 'r') as f:
        for line_num, line in enumerate(f):
            parts = line.strip().split('\t')
            if len(parts) < 3:
                print(f"Line {line_num}: Insufficient columns. Skipping.")
                skipped_rows += 1
                continue

            try:
                # Format: from_step, to_step, id1, id2, ...
                f_s = int(parts[0])
                t_s = int(parts[1])
                row_entities = parts[2:]  # All remaining parts are entity IDs
                
                # Validation: Step range
                if f_s < 0 or t_s >= T or f_s > t_s:
                    print(f"Line {line_num}: Invalid range [{f_s}:{t_s}]. Skipping.")
                    skipped_rows += 1
                    continue

                # Validation: Entities
                for eid in row_entities:
                    eid_str = str(eid).strip()
                    if eid_str in id_to_idx:
                        idx = id_to_idx[eid_str]
                        # Apply mask
                        data[idx, f_s : t_s + 1, 3] = 1.0
                        success_count += 1
                    else:
                        print(f"Line {line_num}: Entity '{eid_str}' not found in metadata.")

            except ValueError:
                print(f"Line {line_num}: Steps must be integers. Skipping.")
                skipped_rows += 1

    # 4. Save and Report
    save_path = output_path if output_path else npy_path
    np.save(save_path, data)
    print("-" * 30)
    print(f"Done! Marked {success_count} total entity-segments.")
    print(f"Processed {line_num + 1} lines, skipped {skipped_rows} lines due to range errors.")
    # print(data)

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--dir", type=str, required=True, help="Path to data directory")
    args = parser.parse_args()

    npy_file = os.path.join(args.dir, "entities_1.npy")
    metadata_file = os.path.join(args.dir, "entities_1_npy_metadata.json")
    anomalies_file = os.path.join(args.dir, "anomalies.tsv")
    files = [npy_file, metadata_file, anomalies_file]
    
    for f in files:
        if not os.path.exists(f):
            print(f"Error: File not found -> {f}")
            exit(1)

    update_anomalies_grouped(npy_file, metadata_file, anomalies_file)

    """
    meta = {
        "features": ["posx", "posy", "vis", "an"],
        "entity_ids": [1,2,10,20,50, 60],
        }
    with open("./metedata.json", "w") as f:
        json.dump(meta, f)

    arr = np.zeros((6,10,4), dtype=np.float32)
    np.save("./test.npy", arr)

    # test.tsv
    # 0	1	1	2
    # -1	4	10	20
    # 2	10	10	20
    # 3	5	11	20
    # 4	7	10	10
    # 5	8	1	2
    # 7	9	2	50
    # 8	9	1
    # 9	10	1
    # 3	2	1

    update_anomalies_grouped("test.npy", "metedata.json", "test.tsv")
    """
