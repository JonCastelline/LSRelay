_addon.name = 'LSRelay'
_addon.author = 'Jon'
_addon.version = '1.1.4'
_addon.commands = {'lsrelay', 'lsr'}

require('strings')
require('tables')
require('chat')
local config = require('config')
local socket = require('socket')

-- Default addon configuration
local defaults = {
    tcp_host = '127.0.0.1',
    tcp_port = 45124,
    linkshell_mode = 'ls1', -- 'ls1', 'ls2', or 'both'
    debug = true
}

local settings = config.load(defaults)

-- State variables
local client = nil
local is_connected = false
local last_connect_attempt = 0
local connect_retry_interval = 3.0
local read_buffer = ""

-- Queue for rate-limiting incoming Discord messages into FFXI chat
local chat_queue = {}
local max_queue_size = 20
local last_chat_time = 0
local chat_delay = 0.6 -- 600ms between injected chat lines to prevent drop/kick

-- Fallback JSON stringifier with control code escaping
local function to_json(val)
    local t = type(val)
    if t == 'string' then
        local escaped = val:gsub('\\', '\\\\')
                           :gsub('"', '\\"')
                           :gsub('\n', '\\n')
                           :gsub('\r', '\\r')
                           :gsub('\t', '\\t')
        return '"' .. escaped .. '"'
    elseif t == 'number' or t == 'boolean' then
        return tostring(val)
    elseif t == 'nil' then
        return 'null'
    elseif t == 'table' then
        local is_array = true
        local max_idx = 0
        local count = 0
        for k, _ in pairs(val) do
            count = count + 1
            if type(k) == 'number' and k > 0 and math.floor(k) == k then
                if k > max_idx then max_idx = k end
            else
                is_array = false
            end
        end
        if is_array and max_idx == count then
            local parts = {}
            for i = 1, max_idx do
                table.insert(parts, to_json(val[i]))
            end
            return '[' .. table.concat(parts, ',') .. ']'
        else
            local parts = {}
            for k, v in pairs(val) do
                table.insert(parts, '"' .. tostring(k) .. '":' .. to_json(v))
            end
            return '{' .. table.concat(parts, ',') .. '}'
        end
    else
        return '"' .. tostring(val) .. '"'
    end
end

-- Fast, robust, dependency-free JSON parser for incoming LSRelay messages
local function parse_json_line(str)
    local res = {}
    local pos = 1
    local len = #str
    while pos <= len do
        local key_start, key_end, key = str:find('"([%w_]+)"%s*:%s*', pos)
        if not key_start then break end
        local val_start = key_end + 1
        local first_char = str:sub(val_start, val_start)
        if first_char == '"' then
            local i = val_start + 1
            local escaped = false
            while i <= len do
                local c = str:sub(i, i)
                if c == '\\' and not escaped then
                    escaped = true
                elseif c == '"' and not escaped then
                    break
                else
                    escaped = false
                end
                i = i + 1
            end
            local raw_val = str:sub(val_start + 1, i - 1)
            res[key] = raw_val:gsub('\\"', '"'):gsub('\\\\', '\\'):gsub('\\n', '\n'):gsub('\\r', '\r'):gsub('\\t', '\t')
            pos = i + 1
        else
            local tail = str:sub(val_start)
            local num_str = tail:match('^(%-?%d+%.?%d*)')
            if num_str then
                res[key] = tonumber(num_str)
                pos = val_start + #num_str
            else
                local bool_str = tail:match('^(%a+)')
                if bool_str == 'true' then res[key] = true
                elseif bool_str == 'false' then res[key] = false
                end
                pos = val_start + (bool_str and #bool_str or 1)
            end
        end
    end
    return res
end

local function print_msg(msg)
    windower.add_to_chat(207, '[LSRelay] ' .. msg)
end

local function print_error(msg)
    windower.add_to_chat(167, '[LSRelay Error] ' .. msg)
end

local function send_payload(payload)
    if not client or not is_connected then
        print_error('[DEBUG] Cannot send: socket not connected (is_connected=' .. tostring(is_connected) .. ')')
        return false
    end
    local json_str = to_json(payload) .. '\n'
    print_msg('[DEBUG] Sending: ' .. json_str:gsub('\n', ''))
    
    -- Give send a 100ms window so it never drops on Windows sockets
    client:settimeout(0.1)
    local sent, err = client:send(json_str)
    client:settimeout(0)
    print_msg('[DEBUG] send result: sent=' .. tostring(sent) .. ', err=' .. tostring(err))
    
    if not sent then
        print_error('Send failed: ' .. tostring(err))
        if err == 'closed' then
            is_connected = false
            pcall(function() client:close() end)
            client = nil
        end
        return false
    end
    return true
end

local function disconnect()
    if client then
        pcall(function() client:close() end)
        client = nil
    end
    if is_connected then
        is_connected = false
        print_msg('[DEBUG] Disconnected from Hub.')
    end
    read_buffer = ""
end

local function try_connect()
    disconnect()
    last_connect_attempt = os.clock()

    print_msg('[DEBUG] Connecting to Hub at ' .. settings.tcp_host .. ':' .. settings.tcp_port .. '...')
    local sock, err = socket.tcp()
    if not sock then
        print_error('Failed to create socket: ' .. tostring(err))
        return
    end

    sock:setoption('tcp-nodelay', true)
    sock:settimeout(0.5)
    local res, conn_err = sock:connect(settings.tcp_host, settings.tcp_port)
    print_msg('[DEBUG] connect result: res=' .. tostring(res) .. ', conn_err=' .. tostring(conn_err))

    if res == 1 or conn_err == 'already connected' then
        sock:settimeout(0)
        client = sock
        is_connected = true
        print_msg('Connected to Hub at ' .. settings.tcp_host .. ':' .. settings.tcp_port)
        local player = windower.ffxi.get_player()
        local player_name = player and player.name or 'Unknown'
        print_msg('[DEBUG] Sending hello for: ' .. player_name)
        send_payload({
            type = 'hello',
            player = player_name,
            mode = settings.linkshell_mode
        })
    else
        print_error('[DEBUG] Failed to connect: ' .. tostring(conn_err))
        sock:close()
        client = nil
        is_connected = false
    end
end

local function handle_incoming_server_message(line)
    print_msg('[DEBUG] Received server line: ' .. line)
    local data = parse_json_line(line)
    print_msg('[DEBUG] Parsed type: ' .. tostring(data and data.type))
    if not data or not data.type then
        return
    end

    if data.type == 'config' then
        if data.linkshell_mode and data.linkshell_mode ~= settings.linkshell_mode then
            settings.linkshell_mode = data.linkshell_mode:lower()
            config.save(settings)
            print_msg('Linkshell mode synced from Hub: ' .. settings.linkshell_mode:upper())
        end
    elseif data.type == 'pair_code' then
        local code = data.code or 'UNKNOWN'
        local expires = tonumber(data.expires_in) or 300
        windower.add_to_chat(200, '--------------------------------------------------')
        windower.add_to_chat(200, '[LSRelay] Your Discord pairing code is: ' .. code)
        windower.add_to_chat(200, '[LSRelay] In Discord, type: !lslink ' .. code)
        windower.add_to_chat(200, '[LSRelay] (Code expires in ' .. math.floor(expires / 60) .. ' minutes)')
        windower.add_to_chat(200, '--------------------------------------------------')
    elseif data.type == 'inject_chat' then
        local mode = (data.mode or 'ls1'):lower()
        local msg = data.message or ''
        if msg ~= '' then
            if #chat_queue >= max_queue_size then
                table.remove(chat_queue, 1)
            end
            table.insert(chat_queue, {
                mode = mode,
                message = msg
            })
            print_msg('Queued chat for ' .. mode:upper() .. ': ' .. msg)
        end
    end
end

local function poll_socket()
    if not client or not is_connected then
        if (os.clock() - last_connect_attempt) >= connect_retry_interval then
            try_connect()
        end
        return
    end

    -- Read data from server line by line
    while true do
        local line, err, partial = client:receive('*l')
        local full_line = nil
        if line then
            if read_buffer ~= "" then
                full_line = read_buffer .. line
                read_buffer = ""
            else
                full_line = line
            end
        elseif partial and #partial > 0 then
            read_buffer = read_buffer .. partial
            local nl = read_buffer:find('\n')
            if nl then
                full_line = read_buffer:sub(1, nl - 1)
                read_buffer = read_buffer:sub(nl + 1)
            end
        end

        if full_line then
            handle_incoming_server_message(full_line:gsub('\r$', ''))
        else
            if err == 'closed' then
                print_msg('Hub disconnected.')
                disconnect()
            end
            break
        end
    end
end

local function process_chat_queue()
    if #chat_queue == 0 then
        return
    end

    local info = windower.ffxi.get_info()
    if not info or not info.logged_in or info.zone == 0 then
        return
    end

    local now = os.clock()
    if (now - last_chat_time) < chat_delay then
        return
    end

    local item = table.remove(chat_queue, 1)
    local prefix = (item.mode == 'ls2') and '/l2 ' or '/l '
    local cmd = prefix .. item.message

    windower.chat.input(cmd)
    last_chat_time = now
end

-- Windower Render Loop
windower.register_event('prerender', function()
    poll_socket()
    process_chat_queue()
end)

-- Listen for in-game Linkshell chat messages
windower.register_event('chat message', function(message, sender, mode, gm)
    local ls_mode = nil
    if mode == 5 then
        ls_mode = 'ls1'
    elseif mode == 27 then
        ls_mode = 'ls2'
    else
        return
    end

    local active_mode = settings.linkshell_mode:lower()
    if active_mode ~= 'both' and active_mode ~= ls_mode then
        return
    end

    local clean_msg = message
    if clean_msg.strip_format then
        clean_msg = clean_msg:strip_format()
    end

    send_payload({
        type = 'chat',
        mode = ls_mode,
        sender = sender,
        message = clean_msg
    })
end)

-- In-game addon commands
windower.register_event('addon command', function(command, ...)
    local args = {...}
    command = (command or 'status'):lower()

    if command == 'status' then
        local player = windower.ffxi.get_player()
        print_msg('Status:')
        print_msg('  Character: ' .. (player and player.name or 'Unknown'))
        print_msg('  Hub Server: ' .. settings.tcp_host .. ':' .. settings.tcp_port)
        print_msg('  Connection: ' .. (is_connected and 'Connected' or 'Disconnected'))
        print_msg('  Linkshell Mode: ' .. settings.linkshell_mode:upper())
        print_msg('  Queued Messages: ' .. tostring(#chat_queue))
    elseif command == 'link' then
        if not is_connected then
            print_error('Not connected to LSRelay Hub. Type //lsr reconnect or check host.')
            return
        end
        local player = windower.ffxi.get_player()
        if not player or not player.name then
            print_error('Unable to determine character name. Please ensure you are logged into FFXI.')
            return
        end
        print_msg('Requesting pairing code for ' .. player.name .. '...')
        send_payload({
            type = 'request_pair_code',
            player = player.name
        })
    elseif command == 'host' then
        local new_host = args[1]
        if not new_host or new_host == '' then
            print_error('Usage: //lsr host <ip-or-domain>')
            return
        end
        settings.tcp_host = new_host
        config.save(settings)
        print_msg('Hub host set to: ' .. new_host .. '. Reconnecting...')
        try_connect()
    elseif command == 'port' then
        local new_port = tonumber(args[1])
        if not new_port or new_port <= 0 or new_port > 65535 then
            print_error('Usage: //lsr port <number>')
            return
        end
        settings.tcp_port = new_port
        config.save(settings)
        print_msg('Hub port set to: ' .. tostring(new_port) .. '. Reconnecting...')
        try_connect()
    elseif command == 'mode' then
        local new_mode = args[1] and args[1]:lower()
        if new_mode ~= 'ls1' and new_mode ~= 'ls2' and new_mode ~= 'both' then
            print_error('Usage: //lsr mode <ls1|ls2|both>')
            return
        end
        settings.linkshell_mode = new_mode
        config.save(settings)
        print_msg('Linkshell mode set to: ' .. new_mode:upper())
        send_payload({
            type = 'config',
            linkshell_mode = new_mode
        })
    elseif command == 'reconnect' then
        print_msg('Reconnecting to Hub...')
        try_connect()
    elseif command == 'debug' then
        settings.debug = not settings.debug
        config.save(settings)
        print_msg('Debug mode: ' .. (settings.debug and 'ON' or 'OFF'))
    else
        print_msg('Commands:')
        print_msg('  //lsr link - Request a 5-minute pairing code to link with Discord')
        print_msg('  //lsr status - View Hub connection status and settings')
        print_msg('  //lsr host <host> - Set Hub IP or domain')
        print_msg('  //lsr port <port> - Set Hub TCP port')
        print_msg('  //lsr mode <ls1|ls2|both> - Set active linkshell')
        print_msg('  //lsr reconnect - Force reconnect to Hub')
        print_msg('  //lsr debug - Toggle debug output')
    end
end)

windower.register_event('load', function()
    try_connect()
end)

windower.register_event('login', function(name)
    if is_connected then
        local player_name = name or (windower.ffxi.get_player() and windower.ffxi.get_player().name) or 'Unknown'
        send_payload({
            type = 'hello',
            player = player_name,
            mode = settings.linkshell_mode
        })
    else
        try_connect()
    end
end)

windower.register_event('unload', function()
    disconnect()
end)
