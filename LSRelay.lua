_addon.name = 'LSRelay'
_addon.author = 'Jon'
_addon.version = '1.1.0'
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
    debug = false
}

local settings = config.load(defaults)

-- State variables
local client = nil
local is_connected = false
local last_connect_attempt = 0
local connect_retry_interval = 3.0
local connect_timeout = 5.0
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
        -- Escape backslashes, quotes, and control characters (RFC 8259)
        local escaped = val:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('[\x00-\x1f]', function(c)
            if c == '\n' then return '\\n'
            elseif c == '\r' then return '\\r'
            elseif c == '\t' then return '\\t'
            else return '' end
        end)
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

-- Fast, robust JSON parser for incoming LSRelay messages
local function parse_json_line(str)
    -- Try Windower's json library if available
    local ok, json = pcall(require, 'json')
    if ok and json and json.parse then
        local parsed = json.parse(str)
        if parsed then return parsed end
    end

    -- Fallback parser for standard string fields handling escaped quotes
    local res = {}
    local pattern = '"([%w_]+)"%s*:%s*"(.-)"[%,%}]'
    for key, val in str:gmatch(pattern) do
        res[key] = val:gsub('\\"', '"'):gsub('\\\\', '\\')
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
        return false
    end
    local json_str = to_json(payload) .. '\n'
    local sent, err = client:send(json_str)
    if not sent then
        if settings.debug then
            print_error('Send failed: ' .. tostring(err))
        end
        is_connected = false
        pcall(function() client:close() end)
        client = nil
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
        if settings.debug then
            print_msg('Disconnected from Hub.')
        end
    end
    read_buffer = ""
end

local function try_connect()
    disconnect()
    last_connect_attempt = os.clock()

    local sock, err = socket.tcp()
    if not sock then
        if settings.debug then
            print_error('Failed to create socket: ' .. tostring(err))
        end
        return
    end

    sock:settimeout(0) -- Non-blocking
    local res, conn_err = sock:connect(settings.tcp_host, settings.tcp_port)
    client = sock

    if res == 1 or conn_err == 'already connected' then
        is_connected = true
        print_msg('Connected to Hub at ' .. settings.tcp_host .. ':' .. settings.tcp_port)
        local player = windower.ffxi.get_player()
        send_payload({
            type = 'hello',
            player = player and player.name or 'Unknown',
            mode = settings.linkshell_mode
        })
    elseif conn_err == 'timeout' or conn_err == 'operation already in progress' then
        -- Connection will be verified in select loop
    else
        -- Refused or immediate error
        disconnect()
    end
end

local function handle_incoming_server_message(line)
    local data = parse_json_line(line)
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
        local expires = data.expires_in or 300
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
            if settings.debug then
                print_msg('Queued chat for ' .. mode:upper() .. ': ' .. msg)
            end
        end
    end
end

local function poll_socket()
    if not client then
        if (os.clock() - last_connect_attempt) >= connect_retry_interval then
            try_connect()
        end
        return
    end

    -- Check for connection timeout while pending connect
    if not is_connected then
        if (os.clock() - last_connect_attempt) > connect_timeout then
            if settings.debug then
                print_error('Connection attempt timed out.')
            end
            disconnect()
            return
        end

        local _, sendt, err = socket.select(nil, {client}, 0)
        if sendt and #sendt > 0 then
            is_connected = true
            print_msg('Connected to Hub at ' .. settings.tcp_host .. ':' .. settings.tcp_port)
            local player = windower.ffxi.get_player()
            send_payload({
                type = 'hello',
                player = player and player.name or 'Unknown',
                mode = settings.linkshell_mode
            })
        elseif err and err ~= 'timeout' then
            disconnect()
            return
        end
    end

    if not is_connected then
        return
    end

    -- Read data from server with proper stream buffering
    while true do
        local chunk, err, partial = client:receive(4096)
        local data = chunk or partial
        if data and #data > 0 then
            read_buffer = read_buffer .. data
            while true do
                local nl_pos = read_buffer:find('\n')
                if not nl_pos then break end
                local line = read_buffer:sub(1, nl_pos - 1):gsub('\r$', '')
                read_buffer = read_buffer:sub(nl_pos + 1)
                handle_incoming_server_message(line)
            end
        end

        if not chunk then
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
        print_msg('  //lsr link - Request a 6-digit code to link your Discord account')
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
