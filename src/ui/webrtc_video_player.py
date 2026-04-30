def get_video_player(
        webrtc_url: str,
        stream_name: str, 
        stun_server: str,
) -> str:

    # MediaMTX WebRTC player HTML with automatic connection
    webrtc_player_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>MediaMTX WebRTC Player</title>
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
                background-color: black;
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
            }}
            #reconnect-btn {{
                position: absolute;
                top: 10px;
                right: 10px;
                background-color: #ff4b4b;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 14px;
                display: none;
            }}
            #reconnect-btn:hover {{
                background-color: #ff6b6b;
            }}
            .loading {{
                color: #ffa500;
            }}
            .connected {{
                color: #00ff00;
            }}
            .error {{
                color: #ff4444;
            }}
        </style>
    </head>
    <body>
        <div id="video-container">
            <video id="video" autoplay muted playsinline></video>
            <div id="status" class="loading">Connecting...</div>
            <button id="reconnect-btn" onclick="reconnect()">Reconnect</button>
        </div>

        <script>
            const video = document.getElementById('video');
            const status = document.getElementById('status');
            const reconnectBtn = document.getElementById('reconnect-btn');
            
            let pc = null;
            let reconnectInterval = null;
            let isConnecting = false;
            
            function updateStatus(message, className = 'loading') {{
                status.textContent = message;
                status.className = className;
                
                if (className === 'error') {{
                    reconnectBtn.style.display = 'block';
                }} else {{
                    reconnectBtn.style.display = 'none';
                }}
            }}
            
            async function startStream() {{
                if (isConnecting) return;
                
                isConnecting = true;
                updateStatus('Connecting to MediaMTX...', 'loading');
                
                try {{
                    // Clean up existing connection
                    if (pc) {{
                        pc.close();
                        pc = null;
                    }}
                    
                    // Create new peer connection
                    pc = new RTCPeerConnection({{
                        iceServers: [{{ urls: '{stun_server}' }}]
                    }});
                    
                    // Handle incoming stream
                    pc.ontrack = (event) => {{
                        console.log('Received track:', event.track.kind);
                        if (event.track.kind === 'video') {{
                            video.srcObject = event.streams[0];
                            updateStatus('Connected', 'connected');
                            clearInterval(reconnectInterval);
                        }}
                    }};
                    
                    pc.oniceconnectionstatechange = () => {{
                        console.log('ICE connection state:', pc.iceConnectionState);
                        switch (pc.iceConnectionState) {{
                            case 'connected':
                                updateStatus('Connected', 'connected');
                                break;
                            case 'disconnected':
                                updateStatus('Disconnected', 'error');
                                scheduleReconnect();
                                break;
                            case 'failed':
                                updateStatus('Connection failed', 'error');
                                scheduleReconnect();
                                break;
                            case 'closed':
                                updateStatus('Connection closed', 'error');
                                break;
                        }}
                    }};
                    
                    pc.onicecandidateerror = (event) => {{
                        console.error('ICE candidate error:', event);
                    }};
                    
                    // Add transceiver for receiving video
                    pc.addTransceiver('video', {{ direction: 'recvonly' }});
                    
                    // Create offer
                    const offer = await pc.createOffer();
                    await pc.setLocalDescription(offer);
                    
                    // Send offer to MediaMTX WHEP endpoint
                    const response = await fetch('{webrtc_url}/{stream_name}/whep', {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/sdp'
                        }},
                        body: offer.sdp
                    }});
                    
                    if (!response.ok) {{
                        throw new Error(`HTTP ${{response.status}}: ${{response.statusText}}`);
                    }}
                    
                    const answerSdp = await response.text();
                    
                    // Set remote description
                    await pc.setRemoteDescription({{
                        type: 'answer',
                        sdp: answerSdp
                    }});
                    
                    updateStatus('Waiting for video...', 'loading');
                    
                }} catch (error) {{
                    console.error('Connection error:', error);
                    updateStatus(`Error: ${{error.message}}`, 'error');
                    scheduleReconnect();
                }}
                
                isConnecting = false;
            }}
            
            function scheduleReconnect() {{
                if (reconnectInterval) return;
                
                let countdown = 5;
                updateStatus(`Reconnecting in ${{countdown}}s...`, 'error');
                
                reconnectInterval = setInterval(() => {{
                    countdown--;
                    if (countdown > 0) {{
                        updateStatus(`Reconnecting in ${{countdown}}s...`, 'error');
                    }} else {{
                        clearInterval(reconnectInterval);
                        reconnectInterval = null;
                        startStream();
                    }}
                }}, 1000);
            }}
            
            function reconnect() {{
                if (reconnectInterval) {{
                    clearInterval(reconnectInterval);
                    reconnectInterval = null;
                }}
                startStream();
            }}
            
            function stopStream() {{
                if (pc) {{
                    pc.close();
                    pc = null;
                }}
                
                if (reconnectInterval) {{
                    clearInterval(reconnectInterval);
                    reconnectInterval = null;
                }}
                
                video.srcObject = null;
                updateStatus('Disconnected', 'error');
            }}
            
            // Auto-start when page loads
            window.addEventListener('load', () => {{
                // Small delay to ensure DOM is ready
                setTimeout(startStream, 500);
            }});
            
            // Clean up on page unload
            window.addEventListener('beforeunload', () => {{
                stopStream();
            }});
        </script>
    </body>
    </html>
    """

    return webrtc_player_html
