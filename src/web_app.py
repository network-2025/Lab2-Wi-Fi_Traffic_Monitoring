#!/usr/bin/env python3
"""
Web Interface for WiFi Traffic Monitoring System
"""

import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from pymongo import MongoClient
from functools import wraps
import threading
import time
import subprocess
import re

# Import our modules
from key_manager import KeyManager
from packet_capture import PacketCapture

# Set up Flask app with correct template and static folders
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'web', 'templates'))
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'web', 'static'))

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

@app.template_filter('strptime')
def _strptime(string, fmt='%Y-%m-%dT%H:%M:%S'):
    return datetime.strptime(string, fmt)

app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_active_interface():
    """Get the active network interface."""
    try:
        # Get the default route to find the primary interface
        result = subprocess.run(['ip', 'route', 'get', '1.1.1.1'], capture_output=True, text=True, check=True)
        match = re.search(r'dev\s+(\S+)', result.stdout)
        if match:
            interface = match.group(1)
            logger.info(f"Automatically detected active interface: {interface}")
            return interface
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        logger.warning("Could not automatically detect interface. Falling back to config.")
        return None

# Load configuration
config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.json')
try:
    with open(config_path, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    config = {'capture': {'interface': 'eth0'}}  # Default fallback

# Configuration
MONGODB_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
DB_NAME = os.environ.get('DB_NAME', 'traffic_monitor')
# Automatically detect interface, with fallback to config file
detected_interface = get_active_interface()
INTERFACE = os.environ.get('INTERFACE', detected_interface or config['capture']['interface'])

# Initialize components
key_manager = KeyManager()
packet_capture = None
mongo_client = None
db = None

def init_mongodb():
    """Initialize MongoDB connection"""
    global mongo_client, db
    try:
        mongo_client = MongoClient(MONGODB_URI)
        db = mongo_client[DB_NAME]
        logger.info("MongoDB connection established")
    except Exception as e:
        logger.warning(f"MongoDB connection failed: {e} - Running without database")
        mongo_client = None
        db = None

def init_packet_capture():
    """Initialize packet capture"""
    global packet_capture
    try:
        packet_capture = PacketCapture(
            interface=INTERFACE,
            mongodb_uri=MONGODB_URI,
            db_name=DB_NAME
        )
        logger.info("Packet capture initialized")
    except Exception as e:
        logger.error(f"Packet capture initialization failed: {e}")

def login_required(f):
    """Decorator to require login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to require admin role"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('Admin access required', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    """Home page"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not username or not password:
            flash('Username and password required', 'error')
            return render_template('login.html')
        
        if key_manager.authenticate_user(username, password):
            user_data = key_manager.users_db[username]
            session['user_id'] = username
            session['role'] = user_data['role']
            flash('Login successful', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """Logout"""
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard"""
    try:
        # Get statistics
        stats = get_traffic_stats()
        
        # Get recent packets
        recent_packets = get_recent_packets(limit=20)
        
        # Get capture status
        capture_status = {
            'is_running': packet_capture.is_running if packet_capture else False,
            'interface': INTERFACE
        }
        
        return render_template('dashboard.html', 
                             stats=stats, 
                             recent_packets=recent_packets,
                             capture_status=capture_status)
    except Exception as e:
        logger.error(f"Error in dashboard: {e}")
        flash('Error loading dashboard', 'error')
        return render_template('dashboard.html', stats={}, recent_packets=[])

@app.route('/api/stats')
@login_required
def api_stats():
    """API endpoint for statistics"""
    try:
        stats = get_traffic_stats()
        return jsonify(stats)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/packets')
@login_required
def api_packets():
    """API endpoint for packets"""
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        protocol = request.args.get('protocol')
        src_ip = request.args.get('src_ip')
        dest_ip = request.args.get('dest_ip')
        
        packets = get_packets_filtered(page, limit, protocol, src_ip, dest_ip)
        return jsonify(packets)
    except Exception as e:
        logger.error(f"Error getting packets: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/capture/start', methods=['POST'])
@admin_required
def api_start_capture():
    """Start packet capture"""
    try:
        if packet_capture and not packet_capture.is_running:
            packet_capture.start_capture()
            return jsonify({'status': 'started'})
        else:
            return jsonify({'error': 'Capture already running or not initialized'}), 400
    except Exception as e:
        logger.error(f"Error starting capture: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/capture/stop', methods=['POST'])
@admin_required
def api_stop_capture():
    """Stop packet capture"""
    try:
        if packet_capture and packet_capture.is_running:
            packet_capture.stop_capture()
            return jsonify({'status': 'stopped'})
        else:
            return jsonify({'error': 'Capture not running'}), 400
    except Exception as e:
        logger.error(f"Error stopping capture: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/traffic')
@login_required
def traffic():
    """Traffic monitoring page"""
    return render_template('traffic.html')

@app.route('/admin')
@admin_required
def admin():
    """Admin panel"""
    try:
        users = key_manager.list_users()
        return render_template('admin.html', users=users, now=datetime.utcnow())
    except Exception as e:
        logger.error(f"Error in admin panel: {e}")
        flash('Error loading admin panel', 'error')
        return render_template('admin.html', users=[])

@app.route('/admin/create_user', methods=['POST'])
@admin_required
def create_user():
    """Create new user"""
    try:
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role', 'user')
        expires_days = int(request.form.get('expires_days', 30))
        
        if not username or not password:
            flash('Username and password required', 'error')
            return redirect(url_for('admin'))
        
        key_manager.create_user(username, password, role, expires_days)
        flash(f'User {username} created successfully', 'success')
        
    except ValueError as e:
        flash(str(e), 'error')
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        flash('Error creating user', 'error')
    
    return redirect(url_for('admin'))

@app.route('/admin/deactivate_user/<username>', methods=['POST'])
@admin_required
def deactivate_user(username):
    """Deactivate user"""
    try:
        if username == session['user_id']:
            flash('Cannot deactivate yourself', 'error')
            return redirect(url_for('admin'))
        
        key_manager.deactivate_user(username)
        flash(f'User {username} deactivated', 'success')
        
    except Exception as e:
        logger.error(f"Error deactivating user: {e}")
        flash('Error deactivating user', 'error')
    
    return redirect(url_for('admin'))

@app.route('/api/realtime_stats')
@login_required
def api_realtime_stats():
    """Real-time statistics for dashboard updates"""
    try:
        # Get recent activity (last 5 minutes)
        five_minutes_ago = datetime.now() - timedelta(minutes=5)
        
        pipeline = [
            {
                '$match': {
                    'timestamp': {'$gte': five_minutes_ago.isoformat()}
                }
            },
            {
                '$group': {
                    '_id': '$protocol',
                    'count': {'$sum': 1},
                    'total_bytes': {'$sum': '$packet_length'}
                }
            }
        ]
        
        recent_activity = list(db.packets.aggregate(pipeline))
        
        # Get top source IPs
        top_sources = list(db.packets.aggregate([
            {
                '$match': {
                    'timestamp': {'$gte': five_minutes_ago.isoformat()}
                }
            },
            {
                '$group': {
                    '_id': '$src_ip',
                    'packet_count': {'$sum': 1}
                }
            },
            {'$sort': {'packet_count': -1}},
            {'$limit': 5}
        ]))
        
        return jsonify({
            'recent_activity': recent_activity,
            'top_sources': top_sources,
            'capture_running': packet_capture.is_running if packet_capture else False,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error getting realtime stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats/traffic_over_time')
@login_required
def api_traffic_over_time():
    """API endpoint for traffic over time data"""
    try:
        ip_list_str = request.args.get('ips')
        ips = ip_list_str.split(',') if ip_list_str else []

        two_hours_ago = datetime.now() - timedelta(hours=2)
        match_filter = {'timestamp': {'$gte': two_hours_ago.isoformat()}}
        
        if ips:
            match_filter['src_ip'] = {'$in': ips}
            group_id = {
                'ip': '$src_ip',
                'time': {
                    '$dateToString': {
                        'format': '%Y-%m-%d %H:%M',
                        'date': {
                            '$dateFromParts': {
                                'year': {'$year': {'$dateFromString': {'dateString': '$timestamp'}}},
                                'month': {'$month': {'$dateFromString': {'dateString': '$timestamp'}}},
                                'day': {'$dayOfMonth': {'$dateFromString': {'dateString': '$timestamp'}}},
                                'hour': {'$hour': {'$dateFromString': {'dateString': '$timestamp'}}},
                                'minute': {'$multiply': [{'$floor': {'$divide': [{'$minute': {'$dateFromString': {'dateString': '$timestamp'}}}, 10]}}, 10]}
                            }
                        }
                    }
                }
            }
        else:
            group_id = {
                '$dateToString': {
                    'format': '%Y-%m-%d %H:%M',
                    'date': {
                        '$dateFromParts': {
                            'year': {'$year': {'$dateFromString': {'dateString': '$timestamp'}}},
                            'month': {'$month': {'$dateFromString': {'dateString': '$timestamp'}}},
                            'day': {'$dayOfMonth': {'$dateFromString': {'dateString': '$timestamp'}}},
                            'hour': {'$hour': {'$dateFromString': {'dateString': '$timestamp'}}},
                            'minute': {'$multiply': [{'$floor': {'$divide': [{'$minute': {'$dateFromString': {'dateString': '$timestamp'}}}, 10]}}, 10]}
                        }
                    }
                }
            }

        pipeline = [
            {'$match': match_filter},
            {'$group': {'_id': group_id, 'count': {'$sum': 1}}},
            {'$sort': {'_id': 1}}
        ]
        
        results = list(db.packets.aggregate(pipeline))
        return jsonify(results)
    except Exception as e:
        logger.error(f"Error getting traffic over time: {e}")
        return jsonify({'error': str(e)}), 500

def get_traffic_stats():
    """Get traffic statistics"""
    try:
        total_packets = db.packets.count_documents({})
        logger.info(f"Found {total_packets} total packets in the database.")
        
        # Get protocol distribution
        protocol_pipeline = [
            {'$group': {'_id': '$protocol', 'count': {'$sum': 1}}},
            {'$sort': {'count': -1}}
        ]
        protocol_stats = list(db.packets.aggregate(protocol_pipeline))
        
        # Get traffic activity for last 2 hours in 10-minute intervals
        two_hours_ago = datetime.now() - timedelta(hours=2)
        hourly_pipeline = [
            {
                '$match': {
                    'timestamp': {'$gte': two_hours_ago.isoformat()}
                }
            },
            {
                '$addFields': {
                    'dateObj': {'$dateFromString': {'dateString': '$timestamp'}},
                }
            },
            {
                '$addFields': {
                    'minute10': {
                        '$multiply': [
                            {'$floor': {'$divide': [{'$minute': '$dateObj'}, 10]}},
                            10
                        ]
                    }
                }
            },
            {
                '$group': {
                    '_id': {
                        '$dateToString': {
                            'format': '%Y-%m-%d %H:%M',
                            'date': {
                                '$dateFromParts': {
                                    'year': {'$year': '$dateObj'},
                                    'month': {'$month': '$dateObj'},
                                    'day': {'$dayOfMonth': '$dateObj'},
                                    'hour': {'$hour': '$dateObj'},
                                    'minute': '$minute10'
                                }
                            }
                        }
                    },
                    'count': {'$sum': 1}
                }
            },
            {'$sort': {'_id': 1}}
        ]
        hourly_stats = list(db.packets.aggregate(hourly_pipeline))
        
        # Get top source IPs
        top_sources_pipeline = [
            {'$group': {'_id': '$src_ip', 'count': {'$sum': 1}}},
            {'$sort': {'count': -1}},
            {'$limit': 10}
        ]
        top_sources = list(db.packets.aggregate(top_sources_pipeline))
        
        return {
            'total_packets': total_packets,
            'protocol_stats': protocol_stats,
            'hourly_stats': hourly_stats,
            'top_sources': top_sources
        }
        
    except Exception as e:
        logger.error(f"Error getting traffic stats: {e}")
        return {}

def get_recent_packets(limit=20):
    """Get recent packets"""
    try:
        packets = list(db.packets.find(
            {},
            {'_id': 0}
        ).sort('timestamp', -1).limit(limit))
        
        # Format timestamps for display
        for packet in packets:
            if 'timestamp' in packet:
                try:
                    # Convert ISO timestamp to readable time format
                    dt = datetime.fromisoformat(packet['timestamp'])
                    packet['display_time'] = dt.strftime('%H:%M:%S')
                except:
                    packet['display_time'] = packet['timestamp'][-8:] if len(packet['timestamp']) >= 8 else packet['timestamp']
        
        return packets
        
    except Exception as e:
        logger.error(f"Error getting recent packets: {e}")
        return []

def get_packets_filtered(page=1, limit=50, protocol=None, src_ip=None, dest_ip=None):
    """Get filtered packets with pagination"""
    try:
        # Build filter
        filter_dict = {}
        if protocol:
            filter_dict['protocol'] = protocol
        if src_ip:
            filter_dict['src_ip'] = src_ip
        if dest_ip:
            filter_dict['dest_ip'] = dest_ip
        
        # Calculate skip
        skip = (page - 1) * limit
        
        # Get total count
        total = db.packets.count_documents(filter_dict)
        
        # Get packets
        packets = list(db.packets.find(
            filter_dict,
            {'_id': 0}
        ).sort('timestamp', -1).skip(skip).limit(limit))
        
        return {
            'packets': packets,
            'total': total,
            'page': page,
            'pages': (total + limit - 1) // limit
        }
        
    except Exception as e:
        logger.error(f"Error getting filtered packets: {e}")
        return {'packets': [], 'total': 0, 'page': 1, 'pages': 0}

@app.route('/api/public_key')
@login_required
def api_public_key():
    """Get RSA public key"""
    try:
        public_key = key_manager.get_public_key_pem()
        return jsonify({'public_key': public_key})
    except Exception as e:
        logger.error(f"Error getting public key: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/generate_keys', methods=['POST'])
@admin_required
def api_generate_keys():
    """Generate new RSA keys"""
    try:
        key_manager.generate_keys()
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error generating keys: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/update_user_expiry', methods=['POST'])
@admin_required
def api_update_user_expiry():
    """Update user expiry"""
    try:
        data = request.get_json()
        username = data.get('username')
        expires_days = data.get('expires_days')
        
        if not username or not expires_days:
            return jsonify({'error': 'Username and expires_days required'}), 400
        
        key_manager.update_user_expiry(username, expires_days)
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error updating user expiry: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/stats')
@admin_required
def api_admin_stats():
    """Get admin statistics"""
    try:
        # Get active sessions count
        active_sessions = len(key_manager.active_tokens)
        
        # Get today's packet count
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        packets_today = db.packets.count_documents({
            'timestamp': {'$gte': today.isoformat()}
        })
        
        # Get data volume today
        pipeline = [
            {
                '$match': {
                    'timestamp': {'$gte': today.isoformat()}
                }
            },
            {
                '$group': {
                    '_id': None,
                    'total_bytes': {'$sum': '$packet_length'}
                }
            }
        ]
        
        volume_result = list(db.packets.aggregate(pipeline))
        data_volume = volume_result[0]['total_bytes'] if volume_result else 0
        
        return jsonify({
            'active_sessions': active_sessions,
            'total_logins': 0,  # TODO: Implement login tracking
            'failed_logins': 0,  # TODO: Implement failed login tracking
            'packets_today': packets_today,
            'data_volume': data_volume
        })
        
    except Exception as e:
        logger.error(f"Error getting admin stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/db/clear', methods=['POST'])
@admin_required
def api_clear_db():
    """Clear the packets collection from the database"""
    try:
        db.packets.drop()
        logger.info(f"User {session['user_id']} cleared the packets database.")
        flash('Packet database cleared successfully!', 'success')
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error clearing database: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/health')
def api_health():
    """Health check endpoint"""
    try:
        # Check MongoDB connection
        db.command('ping')
        
        # Check packet capture status
        capture_running = packet_capture.is_running if packet_capture else False
        
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'capture': 'running' if capture_running else 'stopped',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

# Initialize components
try:
    init_mongodb()
    init_packet_capture()
except Exception as e:
    logger.error(f"Initialization error: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, ssl_context='adhoc')
