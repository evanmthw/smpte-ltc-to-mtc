import pyaudio
import audioop
import math
import time
import threading
import mido
import mido.backends.rtmidi
import tkinter as tk

# Initialisation of the LTC listener
FORMAT = pyaudio.paInt16
RATE = 48000
CHUNK = 2048
SYNC_WORD = '0011111111111101'
jam = '00:00:00:00'
now_tc = '00:00:00:00'
last_cam = '-1'
jam_advice = False
jammed = False
codes = [49,50,51,52,53,54,55,56,57,48]
cams = {}

# Thread-safe variables for communication between threads
current_frequency = 24
current_midi_port = ""
midi_output = None  # Persistent MIDI port
midi_lock = threading.Lock()  # Lock for thread-safe MIDI access

# VU meter settings
VU_MIN_DB = -60  # Minimum dB to display
VU_MAX_DB = 0    # Maximum dB (0 dB = full scale)
VU_WIDTH = 200   # Width of the VU meter in pixels
VU_HEIGHT = 20   # Height of the VU meter in pixels

for i,j in enumerate(codes):
    cams[j] = str(i+1)
    
def bin_to_bytes(a,size=1):
    return int(a,2).to_bytes(size,byteorder='little')

def bin_to_int(a):
    return sum(int(j) * 2 ** i for i, j in enumerate(a))

def decode_frame(frame):
    o = {}
    o['frame_units'] = bin_to_int(frame[:4])
    o['user_bits_1'] = int.from_bytes(bin_to_bytes(frame[4:8]),byteorder='little')
    o['frame_tens'] = bin_to_int(frame[8:10])
    o['drop_frame'] = int.from_bytes(bin_to_bytes(frame[10]),byteorder='little')
    o['color_frame'] = int.from_bytes(bin_to_bytes(frame[11]),byteorder='little')
    o['user_bits_2'] = int.from_bytes(bin_to_bytes(frame[12:16]),byteorder='little')
    o['sec_units'] = bin_to_int(frame[16:20])
    o['user_bits_3'] = int.from_bytes(bin_to_bytes(frame[20:24]),byteorder='little')
    o['sec_tens'] = bin_to_int(frame[24:27])
    o['flag_1'] = int.from_bytes(bin_to_bytes(frame[27]),byteorder='little')
    o['user_bits_4'] = int.from_bytes(bin_to_bytes(frame[28:32]),byteorder='little')
    o['min_units'] = bin_to_int(frame[32:36])
    o['user_bits_5'] = int.from_bytes(bin_to_bytes(frame[36:40]),byteorder='little')
    o['min_tens'] = bin_to_int(frame[40:43])
    o['flag_2'] = int.from_bytes(bin_to_bytes(frame[43]),byteorder='little')
    o['user_bits_6'] = int.from_bytes(bin_to_bytes(frame[44:48]),byteorder='little')
    o['hour_units'] = bin_to_int(frame[48:52])
    o['user_bits_7'] = int.from_bytes(bin_to_bytes(frame[52:56]),byteorder='little')
    o['hour_tens'] = bin_to_int(frame[56:58])
    o['bgf'] = int.from_bytes(bin_to_bytes(frame[58]),byteorder='little')
    o['flag_3'] = int.from_bytes(bin_to_bytes(frame[59]),byteorder='little')
    o['user_bits_8'] = int.from_bytes(bin_to_bytes(frame[60:64]),byteorder='little')
    o['sync_word'] = int.from_bytes(bin_to_bytes(frame[64:],2),byteorder='little')
    o['formatted_tc'] = "{:02d}:{:02d}:{:02d}:{:02d}".format(
        o['hour_tens']*10+o['hour_units'],
        o['min_tens']*10+o['min_units'],
        o['sec_tens']*10+o['sec_units'],
        o['frame_tens']*10+o['frame_units'],
    )
    return o

# Thread-safe flag for listening state
listening_active = False

def print_tc():
    global jam, now_tc, listening_active, current_frequency

    freq = current_frequency
    print(freq)
    inter = 1/freq
    last_jam = jam
    h,m,s,f = [int(x) for x in jam.split(':')]
    while listening_active:
        if jam == None:
            break
        if jam != last_jam:
            h,m,s,f = [int(x) for x in jam.split(':')]
            last_jam = jam
        tcp = "{:02d}:{:02d}:{:02d}:{:02d}".format(h,m,s,f)

        if compare_timestamps(tcp,jam) < 1.5:
            send_mtc_signal(tcp)
            frame.after_idle(lambda: status_square.configure(bg="green"))
        else:
            frame.after_idle(lambda: status_square.configure(bg="orange"))
        now_tc = tcp
        time.sleep(inter)
        f += 1
        if f >= freq:
            f = 0
            s += 1
        if s >= 60:
            s = 0
            m += 1
        if m >= 60:
            m = 0
            h += 1

def extract_channel(data, num_channels, channel_index, sample_width=2):
    """Extract a single channel from interleaved multi-channel audio data."""
    if num_channels == 1:
        return data
    
    # Calculate frame size (all channels for one sample)
    frame_size = sample_width * num_channels
    num_frames = len(data) // frame_size
    
    # Extract the selected channel
    extracted = bytearray()
    for i in range(num_frames):
        start = i * frame_size + channel_index * sample_width
        end = start + sample_width
        extracted.extend(data[start:end])
    
    return bytes(extracted)

def decode_ltc(wave_frames):
    global jam
    frames, output, last, toggle, sp = [], '', None, True, 1

    for i in range(0, len(wave_frames), 2):
        data = wave_frames[i:i+2]
        cyc = 'Neg' if audioop.minmax(data, 2)[0] < 0 else 'Pos'

        if cyc != last:
            if sp >= 7:
                if sp > 14:
                    bit = '0'
                elif toggle:
                    bit = '1'
                else:
                    bit = ''

                output += bit
                toggle = not toggle if sp <= 14 else True

                if len(output) >= len(SYNC_WORD) and output[-len(SYNC_WORD):] == SYNC_WORD:
                    if len(output) > 80:
                        frame_data = output[-80:]
                        frames.append(frame_data)
                        output = ''
                        jam = decode_frame(frame_data)['formatted_tc']
                        send_mtc_signal(jam)
            sp = 1
        else:
            sp += 1
        last = cyc

def update_vu_meter(volume_db):
    """Update the VU meter display based on volume in dB."""
    # Clamp the value to our display range
    if math.isinf(volume_db) or volume_db < VU_MIN_DB:
        volume_db = VU_MIN_DB
    elif volume_db > VU_MAX_DB:
        volume_db = VU_MAX_DB
    
    # Calculate the fill width (0 to VU_WIDTH)
    db_range = VU_MAX_DB - VU_MIN_DB
    fill_ratio = (volume_db - VU_MIN_DB) / db_range
    fill_width = int(fill_ratio * VU_WIDTH)
    
    # Determine color based on level
    # Green: -60 to -12 dB (good signal)
    # Yellow: -12 to -6 dB (strong signal)
    # Red: -6 to 0 dB (too hot / clipping risk)
    if volume_db < -12:
        color = "#00cc00"  # Green
    elif volume_db < -6:
        color = "#cccc00"  # Yellow
    else:
        color = "#cc0000"  # Red
    
    # Update the meter
    vu_canvas.delete("level")
    if fill_width > 0:
        vu_canvas.create_rectangle(0, 0, fill_width, VU_HEIGHT, fill=color, tags="level")
    
    # Update the dB text
    if math.isinf(volume_db) or volume_db <= VU_MIN_DB:
        label_volume.config(text="Level: -∞ dB")
    else:
        label_volume.config(text=f"Level: {round(volume_db)} dB")

def loop_decode_ltc(stream, frames, num_channels, channel_index):
    global listening_active
    if not listening_active:
        return
        
    data = stream.read(CHUNK, exception_on_overflow=False)
    
    # Extract the selected channel from multi-channel audio
    mono_data = extract_channel(data, num_channels, channel_index)

    volume_db = get_volume_db(mono_data)
    
    # Update VU meter (must be done from main thread)
    update_vu_meter(volume_db)

    decode_ltc(mono_data)
    frames.append(mono_data)
    if listening_active:
        frame.after(10, lambda: loop_decode_ltc(stream, frames, num_channels, channel_index))

def open_midi_port(port_name):
    """Open the MIDI port and keep it open."""
    global midi_output
    try:
        midi_output = mido.open_output(port_name)
        return True
    except (IOError, ValueError) as e:
        print(f"Error opening MIDI port: {e}")
        return False

def close_midi_port():
    """Close the MIDI port."""
    global midi_output
    if midi_output is not None:
        try:
            midi_output.close()
        except:
            pass
        midi_output = None

def init_ltc_listener():
    global current_frequency, current_midi_port, listening_active
    
    micro_selectionne = selected_microphone_index.get()
    channel_index = selected_channel_index.get()
    num_channels = device_channel_count.get()
    
    # Copy tkinter variables to thread-safe globals before starting thread
    current_frequency = str_frequency_to_int(selected_frequency.get())
    current_midi_port = selected_midi.get()
    listening_active = True
    
    # Open the MIDI port once
    if not open_midi_port(current_midi_port):
        listening_active = False
        status_square.configure(bg="red")
        return

    p = pyaudio.PyAudio()
    t = threading.Thread(target=print_tc, daemon=True)
    t.start()

    stream = p.open(format=FORMAT,
                    channels=num_channels,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK,
                    input_device_index=micro_selectionne)

    frames = []
    loop_decode_ltc(stream, frames, num_channels, channel_index)

# Write in MIDI port the MTC values
def send_mtc_signal(timecode_str):
    global midi_output, current_frequency
    frequency = current_frequency

    # Verify timecode format (HH:MM:SS:FF)
    try:
        hours, minutes, seconds, frames = map(int, timecode_str.split(':'))
    except (ValueError, IndexError):
        return  # Silently fail on bad format

    # Verify validity of numbers
    if not 0 <= hours < 24 or not 0 <= minutes < 60 or not 0 <= seconds < 60 or not 0 <= frames < 30:
        return  # Silently fail on bad values

    # Update timecode label from main thread
    frame.after_idle(lambda tc=timecode_str: label_timecode.config(text=f"Timecode : {tc}"))

    # Calculating complete MTC message
    mtc_hours = decimal_to_hex_pair(hours)
    mtc_minutes = decimal_to_hex_pair(minutes)
    mtc_seconds = decimal_to_hex_pair(seconds)
    mtc_frames = decimal_to_hex_pair(frames)

    # Manual frequency selector
    if frequency == 24:
        mtc_frequency = 0
    elif frequency == 25:
        mtc_frequency = 1
    elif frequency == 30:
        mtc_frequency = 2
    else:
        mtc_frequency = 0

    # Use lock to ensure thread-safe MIDI access
    with midi_lock:
        if midi_output is None:
            return
        try:
            # Send MIDI messages
            midi_output.send(mido.Message('quarter_frame', frame_type=0, frame_value=mtc_frames[1]))
            midi_output.send(mido.Message('quarter_frame', frame_type=1, frame_value=mtc_frames[0]))
            midi_output.send(mido.Message('quarter_frame', frame_type=2, frame_value=mtc_seconds[1]))
            midi_output.send(mido.Message('quarter_frame', frame_type=3, frame_value=mtc_seconds[0]))
            midi_output.send(mido.Message('quarter_frame', frame_type=4, frame_value=mtc_minutes[1]))
            midi_output.send(mido.Message('quarter_frame', frame_type=5, frame_value=mtc_minutes[0]))
            midi_output.send(mido.Message('quarter_frame', frame_type=6, frame_value=mtc_hours[1]))
            midi_output.send(mido.Message('quarter_frame', frame_type=7, frame_value=mtc_frequency))
        except (IOError, ValueError) as e:
            print(f"Error sending MIDI: {e}")

def decimal_to_hex_pair(decimal_value):
    binary_value = bin(decimal_value)[2:].zfill(8)

    first_4_bits = binary_value[:4]
    decimal_value_1 = int(first_4_bits, 2)

    last_4_bits = binary_value[4:]
    decimal_value_2 = int(last_4_bits, 2)

    return [decimal_value_1, decimal_value_2]

def time_to_seconds(time):
    hh, mm, ss, ff = map(int, time.split(':'))
    total_seconds = hh * 3600 + mm * 60 + ss + ff / 30
    return total_seconds

def compare_timestamps(timestamp1, timestamp2):
    return time_to_seconds(timestamp1) - time_to_seconds(timestamp2)

# Get available microphones list with their channel counts
def get_microphone_info():
    """Returns list of (name, index, max_channels) tuples for input devices."""
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    num_devices = info.get('deviceCount')

    microphones = []
    for i in range(num_devices):
        device_info = p.get_device_info_by_index(i)
        if device_info.get('maxInputChannels') > 0:
            microphones.append({
                'name': device_info['name'],
                'index': i,
                'channels': device_info['maxInputChannels']
            })
    
    # Get default device and move it to front
    try:
        default_index = p.get_default_input_device_info()['index']
        for i, mic in enumerate(microphones):
            if mic['index'] == default_index:
                microphones.insert(0, microphones.pop(i))
                break
    except:
        pass
    
    p.terminate()
    return microphones

def get_available_microphones():
    return [mic['name'] for mic in microphone_info]

# Get available output MIDI ports
def get_available_midis():
    ports = []
    for port in mido.get_output_names():
        ports.append(port)
    return ports

# Convert string from frequency selector to extract only the integer value
def str_frequency_to_int(str):
    if str == "24 Hz":
        return 24
    elif str == "25 Hz":
        return 25
    elif str == "30 Hz":
        return 30
    else:
        return 24
    
def get_volume_db(data: bytes, sample_width: int = 2) -> float:
    try:
        rms = audioop.rms(data, sample_width)
        if rms > 0:
            # Convert to dB relative to full scale (16-bit audio max is 32767)
            return 20 * math.log10(rms / 32767)
        else:
            return float('-inf')
    except Exception as e:
        print(f"Erreur lors du calcul du volume : {e}")
        return float('-inf')

def update_channel_options(*args):
    """Update channel dropdown when microphone selection changes."""
    mic_name = selected_microphone.get()
    
    # Find the selected microphone's info
    for mic in microphone_info:
        if mic['name'] == mic_name:
            selected_microphone_index.set(mic['index'])
            device_channel_count.set(mic['channels'])
            
            # Update channel dropdown options
            channel_options = [f"Channel {i+1}" for i in range(mic['channels'])]
            
            # Clear and rebuild the channel menu
            channel_menu['menu'].delete(0, 'end')
            for ch in channel_options:
                channel_menu['menu'].add_command(
                    label=ch, 
                    command=tk._setit(selected_channel, ch)
                )
            
            # Reset to channel 1
            selected_channel.set(channel_options[0])
            selected_channel_index.set(0)
            break

def update_channel_index(*args):
    """Update channel index when channel selection changes."""
    ch_str = selected_channel.get()
    # Extract channel number from "Channel X" string
    try:
        ch_num = int(ch_str.split()[-1]) - 1  # Convert to 0-based index
        selected_channel_index.set(ch_num)
    except:
        selected_channel_index.set(0)

# Toggle LTC Listener from button
def toggle_read_ltc():
    global listening_active
    
    if not listening_active:
        # Starting listener
        listening_active = True
        status_square.configure(bg="orange")
        toggle_button.configure(text="Disable listener")
        label_microphone.configure(state="disabled")
        label_frequency.configure(state="disabled")
        label_midi.configure(state="disabled")
        channel_menu.configure(state="disabled")
        init_ltc_listener()
    else:
        # Stopping listener
        listening_active = False
        close_midi_port()  # Close the MIDI port when stopping
        status_square.configure(bg="red")
        toggle_button.configure(text="Enable listener")
        label_microphone.configure(state="normal")
        label_frequency.configure(state="normal")
        label_midi.configure(state="normal")
        channel_menu.configure(state="normal")
        # Reset VU meter
        vu_canvas.delete("level")
        label_volume.config(text="Level: -- dB")

# Get microphone info (name, index, channels)
microphone_info = get_microphone_info()
microphones_options = get_available_microphones()
frequencies_options = ["24 Hz", "25 Hz", "30 Hz"]
midis_options = get_available_midis()

# Initial channel options based on first microphone
initial_channels = microphone_info[0]['channels'] if microphone_info else 1
channel_options = [f"Channel {i+1}" for i in range(initial_channels)]

# Create main frame
frame = tk.Tk()
frame.title("SMPTE LTC to MTC 1.1.0")
frame.geometry("300x600")
frame.resizable(width=False, height=False)

# Define variables from tk
selected_microphone = tk.StringVar(value=microphones_options[0])
selected_frequency = tk.StringVar(value=frequencies_options[0])
selected_midi = tk.StringVar(value=midis_options[0])
selected_microphone_index = tk.IntVar(value=microphone_info[0]['index'] if microphone_info else 0)
selected_channel = tk.StringVar(value=channel_options[0])
selected_channel_index = tk.IntVar(value=0)
device_channel_count = tk.IntVar(value=initial_channels)
enable_listening = tk.BooleanVar(value=False)
status_color = tk.StringVar(value="Red")

# Set up trace to update channels when microphone changes
selected_microphone.trace('w', update_channel_options)
selected_channel.trace('w', update_channel_index)

# Configure grid to center elements
for i in range(16):
    frame.grid_rowconfigure(i, weight=1)
    frame.grid_columnconfigure(i, weight=1)

# Draw status square
status_square = tk.Canvas(frame, width=50, height=50, bg="red")
status_square.grid(row=0, column=4, pady=10, sticky="n")

# Draw microphone selector
label_microphone_title = tk.Label(frame, text="Select microphone", font=("Helvetica", 10, "bold"))
label_microphone_title.grid(row=1, column=4, pady=5, sticky="n")
label_microphone = tk.OptionMenu(frame, selected_microphone, *microphones_options)
label_microphone.grid(row=2, column=4, pady=5, sticky="n")

# Draw channel selector
label_channel_title = tk.Label(frame, text="Select input channel", font=("Helvetica", 10, "bold"))
label_channel_title.grid(row=3, column=4, pady=5, sticky="n")
channel_menu = tk.OptionMenu(frame, selected_channel, *channel_options)
channel_menu.grid(row=4, column=4, pady=5, sticky="n")

# Draw frequency selector
label_frequency_title = tk.Label(frame, text="Select frequency", font=("Helvetica", 10, "bold"))
label_frequency_title.grid(row=5, column=4, pady=5, sticky="n")
label_frequency = tk.OptionMenu(frame, selected_frequency, *frequencies_options)
label_frequency.grid(row=6, column=4, pady=5, sticky="n")

# Draw MIDI output selector
label_midi_title = tk.Label(frame, text="Select MIDI output", font=("Helvetica", 10, "bold"))
label_midi_title.grid(row=7, column=4, pady=5, sticky="n")
label_midi = tk.OptionMenu(frame, selected_midi, *midis_options)
label_midi.grid(row=8, column=4, pady=5, sticky="n")

# Draw toggle button
toggle_button = tk.Button(frame, text="Enable listener", command=toggle_read_ltc)
toggle_button.grid(row=9, column=4, pady=10, sticky="n")

# Draw timecode
label_timecode = tk.Label(frame, text="Timecode", font=("Helvetica", 10, "bold"))
label_timecode.grid(row=10, column=4, pady=10, sticky="n")

# Draw VU meter section
label_vu_title = tk.Label(frame, text="Input Level", font=("Helvetica", 10, "bold"))
label_vu_title.grid(row=11, column=4, pady=(10, 2), sticky="n")

# VU meter canvas with background
vu_frame = tk.Frame(frame, bg="#333333", padx=2, pady=2)
vu_frame.grid(row=12, column=4, pady=2, sticky="n")

vu_canvas = tk.Canvas(vu_frame, width=VU_WIDTH, height=VU_HEIGHT, bg="#1a1a1a", highlightthickness=0)
vu_canvas.pack()

# Draw reference markers on VU meter
vu_markers = tk.Canvas(frame, width=VU_WIDTH + 4, height=15, bg=frame.cget('bg'), highlightthickness=0)
vu_markers.grid(row=13, column=4, pady=0, sticky="n")

# Add dB scale markers
db_marks = [(-60, "60"), (-48, "48"), (-36, "36"), (-24, "24"), (-12, "12"), (-6, "6"), (0, "0")]
for db, label in db_marks:
    x_pos = int(((db - VU_MIN_DB) / (VU_MAX_DB - VU_MIN_DB)) * VU_WIDTH) + 2
    vu_markers.create_text(x_pos, 8, text=label, font=("Helvetica", 7), fill="#666666")

# Draw volume label
label_volume = tk.Label(frame, text="Level: -- dB", font=("Helvetica", 9))
label_volume.grid(row=14, column=4, pady=5, sticky="n")

# Cleanup on window close
def on_closing():
    global listening_active
    listening_active = False
    close_midi_port()
    frame.destroy()

frame.protocol("WM_DELETE_WINDOW", on_closing)

# Autostart
#toggle_read_ltc()

# Starting main loop
frame.mainloop()
