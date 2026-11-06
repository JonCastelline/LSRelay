_addon.name = 'LSRelay'
_addon.author = 'Jon'
_addon.version = '1.0.0'
_addon.commands = {'lsrelay', 'lsr'}

require('strings')
require('tables')
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
local read_buffer = ""

-- Queue for rate-limiting incoming Discord messages into FFXI chat
local chat_queue = {}
local last_chat_time = 0
local chat_delay = 0.6 -- 600ms between injected chat lines to prevent drop/kick

-- Fallback JSON stringifier
local function to_json(val)
    local t = type(val)
    if t == 'string' then
        return '"' .. val:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r') .. '"'
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

-- Fast, robust JSON parser for expected LSRelay messages
local function parse_json_line(str)
    -- Try Windower's json library if available
    local ok, json = pcall(require, 'json')
    if ok and json and json.parse then
        local parsed = json.parse(str)
        if parsed then return parsed end
    end

    -- Fallback parser for standard string fields
    local res = {}
    for key, val in str:gmatch('"([%w_]+)"%s*:%s*"([^"]*)"') do
        res[key] = val
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
            print_msg('Disconnected from Python relay.')
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
        print_msg('Connected to Discord relay at ' .. settings.tcp_host .. ':' .. settings.tcp_port)
        -- Send greeting
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
            print_msg('Linkshell mode synced from relay: ' .. settings.linkshell_mode:upper())
        end
    elseif data.type == 'inject_chat' then
        local mode = (data.mode or 'ls1'):lower()
        local msg = data.message or ''
        if msg ~= '' then
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

    if not is_connected then
        -- Check if non-blocking connect has completed
        local _, sendt, err = socket.select(nil, {client}, 0)
        if sendt and #sendt > 0 then
            is_connected = true
            print_msg('Connected to Discord relay at ' .. settings.tcp_host .. ':' .. settings.tcp_port)
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

    -- Read data from server non-blocking
    while true do
        local chunk, err, partial = client:receive('*l')
        if chunk then
            handle_incoming_server_message(chunk)
        else
            if err == 'closed' then
                print_msg('Discord relay disconnected.')
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

    -- Mode filter
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
        print_msg('Status:')
        print_msg('  Connection: ' .. (is_connected and ('Connected (' .. settings.tcp_host .. ':' .. settings.tcp_port .. ')') or 'Disconnected'))
        print_msg('  Linkshell Mode: ' .. settings.linkshell_mode:upper())
        print_msg('  Queued Messages: ' .. tostring(#chat_queue))
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
        print_msg('Reconnecting to relay...')
        try_connect()
    elseif command == 'debug' then
        settings.debug = not settings.debug
        config.save(settings)
        print_msg('Debug mode: ' .. (settings.debug and 'ON' or 'OFF'))
    else
        print_msg('Commands:')
        print_msg('  //lsr status - View relay connection status and mode')
        print_msg('  //lsr mode <ls1|ls2|both> - Set active linkshell')
        print_msg('  //lsr reconnect - Force reconnect to Python relay')
        print_msg('  //lsr debug - Toggle debug output')
    end
end)

windower.register_event('load', function()
    try_connect()
end)

windower.register_event('unload', function()
    disconnect()
end)
