def get_video_player(
        hls_url: str,
        stream_name: str,
) -> str:
    # MediaMTX HLS player HTML using hls.js
    # hls_url should be something like "http://10.101.13.137:8888"

    hls_player_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>MediaMTX HLS Player</title>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <style>
            body {{
                margin: 0;
                padding: 0;
                background-color: #0e1117;
                font-family: sans-serif;
            }}
            #video-container {{
                width: 100%;
                height: 500px;
                display: flex;
                justify-content: center;
                align-items: center;
                position: relative;
                background-color: black;
            }}
            #video {{
                width: 100%;
                height: 100%;
                object-fit: contain;
            }}
            #status {{
                position: absolute;
                top: 10px;
                left: 10px;
                background-color: rgba(0,0,0,0.8);
                color: white;
                padding: 8px 12px;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
                z-index: 10;
            }}
            .loading {{ color: #ffa500; }}
            .connected {{ color: #00ff00; }}
            .error {{ color: #ff4444; }}
        </style>
    </head>
    <body>
        <div id="video-container">
            <video id="video" controls autoplay muted playsinline></video>
            <div id="status" class="loading">Initializing...</div>
        </div>
        <script>
            const video = document.getElementById('video');
            const status = document.getElementById('status');
            const source = "{hls_url}/{stream_name}/index.m3u8";
            
            function updateStatus(message, className = 'loading') {{
                status.textContent = message;
                status.className = className;
            }}
            function initPlayer() {{
                if (Hls.isSupported()) {{
                    const hls = new Hls({{
                        manifestLoadingMaxRetry: 10,
                        manifestLoadingRetryDelay: 1000,
                    }});
                    
                    hls.loadSource(source);
                    hls.attachMedia(video);
                    
                    hls.on(Hls.Events.MANIFEST_PARSED, () => {{
                        updateStatus('Live', 'connected');
                        video.play();
                    }});
                    hls.on(Hls.Events.ERROR, (event, data) => {{
                        if (data.fatal) {{
                            updateStatus('Stream Offline - Retrying...', 'error');
                            hls.startLoad();
                        }}
                    }});
                }} 
                // For Safari (which has native HLS support)
                else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
                    video.src = source;
                    video.addEventListener('loadedmetadata', () => {{
                        updateStatus('Live', 'connected');
                        video.play();
                    }});
                }}
            }}
            window.addEventListener('load', initPlayer);
        </script>
    </body>
    </html>
    """
    return hls_player_html