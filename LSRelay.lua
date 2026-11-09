_addon.name = 'LSRelay'
_addon.author = 'Frodobald'
_addon.version = '1.4.0'
_addon.commands = {'lsrelay', 'lsr'}

require('strings')
local packets = require('packets')
require('tables')
require('chat')
local res = require('resources')
local extdata = require('extdata')
local config = require('config')
local socket = require('socket')

-- Default addon configuration
local defaults = {
    tcp_host = '127.0.0.1',
    tcp_port = 45124,
    relay_tells = true,
    relay_gm = true,
    gm_unload_addons = 'trust,react,cureplease,autoinvite',
    gm_stop_actions = true,
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
local max_queue_size = 20
local last_chat_time = 0
local chat_delay = 0.6 -- 600ms between injected chat lines to prevent drop/kick

-- Resolve active server name from Windower resources
local function get_server_name()
    local info = windower.ffxi.get_info()
    if info and info.server and res and res.servers and res.servers[info.server] then
        return res.servers[info.server].en
    end
    return nil
end

-- Resolve equipped linkshell names for Slot 1 (/l) and Slot 2 (/l2)
local function get_equipped_linkshells()
    local ls1_name = nil
    local ls2_name = nil

    local items = windower.ffxi.get_items()
    if items and items.equipment and extdata then
        local eq = items.equipment
        -- Slot 1 (/l): pearls can only live in bag 0 (Inventory)
        local slot1_idx = eq.linkshell
        local bag1 = eq.linkshell_bag or 0
        if slot1_idx and slot1_idx > 0 then
            local item = windower.ffxi.get_items(bag1, slot1_idx)
            if item and item.id and item.id > 0 and item.extdata then
                local ok, parsed = pcall(extdata.decode, item)
                if ok and parsed and parsed.name and #parsed.name > 0 then
                    ls1_name = parsed.name
                end
            end
        end

        -- Slot 2 (/l2): pearls can only live in bag 0 (Inventory)
        local slot2_idx = eq.linkshell2
        local bag2 = eq.linkshell2_bag or 0
        if slot2_idx and slot2_idx > 0 then
            local item = windower.ffxi.get_items(bag2, slot2_idx)
            if item and item.id and item.id > 0 and item.extdata then
                local ok, parsed = pcall(extdata.decode, item)
                if ok and parsed and parsed.name and #parsed.name > 0 then
                    ls2_name = parsed.name
                end
            end
        end
    end

    return ls1_name, ls2_name
end

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
        if settings.debug then
            print_error('Cannot send: socket not connected.')
        end
        return false
    end
    local json_str = to_json(payload) .. '\n'
    if settings.debug then
        print_msg('Sending: ' .. json_str:gsub('\n', ''))
    end
    
    -- Give send a 100ms window so it never drops on Windows sockets
    client:settimeout(0.1)
    local sent, err = client:send(json_str)
    client:settimeout(0)
    
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
        if settings.debug then
            print_msg('Disconnected from Hub.')
        end
    end
    read_buffer = ""
end

local function try_connect()
    disconnect()
    last_connect_attempt = os.clock()

    if settings.debug then
        print_msg('Connecting to Hub at ' .. settings.tcp_host .. ':' .. settings.tcp_port .. '...')
    end
    local sock, err = socket.tcp()
    if not sock then
        print_error('Failed to create socket: ' .. tostring(err))
        return
    end

    sock:setoption('tcp-nodelay', true)
    sock:settimeout(0.5)
    local res, conn_err = sock:connect(settings.tcp_host, settings.tcp_port)

    if res == 1 or conn_err == 'already connected' then
        sock:settimeout(0)
        client = sock
        is_connected = true
        print_msg('Connected to Hub at ' .. settings.tcp_host .. ':' .. settings.tcp_port)
        local player = windower.ffxi.get_player()
        local player_name = player and player.name or 'Unknown'
        local server_name = get_server_name()
        send_payload({
            type = 'hello',
            player = player_name,
            server = server_name
        })
    else
        if settings.debug then
            print_error('Failed to connect: ' .. tostring(conn_err))
        end
        sock:close()
        client = nil
        is_connected = false
    end
end

local function handle_incoming_server_message(line)
    if settings.debug then
        print_msg('Received: ' .. line)
    end
    local data = parse_json_line(line)
    if not data or not data.type then
        return
    end

    if data.type == 'pair_code' then
        local code = data.code or 'UNKNOWN'
        local expires = tonumber(data.expires_in) or 300
        windower.add_to_chat(200, '--------------------------------------------------')
        windower.add_to_chat(200, '[LSRelay] Your Discord pairing code is: ' .. code)
        windower.add_to_chat(200, '[LSRelay] In Discord, type: !lslink ' .. code)
        windower.add_to_chat(200, '[LSRelay] (Code expires in ' .. math.floor(expires / 60) .. ' minutes)')
        windower.add_to_chat(200, '--------------------------------------------------')
    elseif data.type == 'inject_chat' then
        local mode = (data.mode or 'ls1'):lower()
        local target = data.target
        local msg = data.message or ''
        if msg ~= '' then
            if #chat_queue >= max_queue_size then
                table.remove(chat_queue, 1)
            end
            table.insert(chat_queue, {
                mode = mode,
                target = target,
                server = data.server,
                linkshell = data.linkshell,
                message = msg
            })
            if settings.debug then
                print_msg('Queued chat for ' .. (data.linkshell or mode):upper() .. ': ' .. msg)
            end
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
    local cmd = nil
    if item.mode == 'tell' then
        cmd = '/t ' .. tostring(item.target) .. ' ' .. item.message
    else
        local ls1_name, ls2_name = get_equipped_linkshells()
        local target_ls = item.linkshell and item.linkshell:lower() or nil
        local use_ls2 = false

        if target_ls and ls2_name and target_ls == ls2_name:lower() then
            use_ls2 = true
        elseif target_ls and ls1_name and target_ls == ls1_name:lower() then
            use_ls2 = false
        else
            use_ls2 = (item.mode == 'ls2')
        end

        local prefix = use_ls2 and '/l2 ' or '/l '
        cmd = prefix .. item.message
    end

    windower.chat.input(cmd)
    last_chat_time = now
end

local function trigger_gm_safety_protocol(sender)
    windower.add_to_chat(167, '==================================================')
    windower.add_to_chat(167, '[LSRelay ALERT] GM MESSAGE RECEIVED FROM: ' .. tostring(sender))
    windower.add_to_chat(167, '[LSRelay ALERT] Executing safety protocol...')
    windower.add_to_chat(167, '==================================================')

    pcall(function() windower.play_sound(windower.addon_path .. 'alert.wav') end)

    if settings.gm_unload_addons and settings.gm_unload_addons ~= '' then
        for addon in settings.gm_unload_addons:gmatch('[^,%s]+') do
            local clean_addon = addon:lower()
            if clean_addon ~= 'lsrelay' and clean_addon ~= 'lsr' then
                windower.send_command('lua unload ' .. clean_addon)
            end
        end
    end

    if settings.gm_stop_actions then
        windower.send_command('input /attack off; input //trust stop')
    end
end

-- Windower Render Loop
windower.register_event('prerender', function()
    poll_socket()
    process_chat_queue()
end)

-- Listen for in-game Linkshell chat messages
windower.register_event('chat message', function(message, sender, mode, gm)
    if settings.debug then
        print_msg('Chat event: mode=' .. tostring(mode) .. ' sender=' .. tostring(sender) .. ' msg=' .. tostring(message))
    end
    -- GM Detection (gm boolean flag, or mode 12/13)
    if gm or mode == 12 or mode == 13 then
        if settings.relay_gm then
            local clean_msg = message
            if clean_msg.strip_format then
                clean_msg = clean_msg:strip_format()
            end
            send_payload({
                type = 'gm',
                sender = sender or 'GM',
                message = clean_msg
            })
            trigger_gm_safety_protocol(sender or 'GM')
        end
        return
    end

    -- Player Tell Detection (mode 3 = incoming tell)
    if mode == 3 then
        if settings.relay_tells then
            local clean_msg = message
            if clean_msg.strip_format then
                clean_msg = clean_msg:strip_format()
            end
            send_payload({
                type = 'tell',
                sender = sender or 'Unknown',
                message = clean_msg
            })
        end
        return
    end

    local ls_mode = nil
    if mode == 5 then
        ls_mode = 'ls1'
    elseif mode == 27 then
        ls_mode = 'ls2'
    else
        return
    end

    local clean_msg = message
    if clean_msg.strip_format then
        clean_msg = clean_msg:strip_format()
    end

    local ls1_name, ls2_name = get_equipped_linkshells()
    local server_name = get_server_name()
    local active_ls = (ls_mode == 'ls2') and ls2_name or ls1_name

    send_payload({
        type = 'chat',
        mode = ls_mode,
        server = server_name,
        linkshell = active_ls,
        sender = sender,
        message = clean_msg
    })
end)




-- Catch own outgoing chat packets (0x0B5 = Linkshell speech)
windower.register_event('outgoing chunk', function(id, data, modified, injected, blocked)
    if blocked or injected then return end

    if id == 0x0B5 then
        local ok, err = pcall(function()
            local p = packets.parse('outgoing', data)
            local mode = p['Mode'] or p['mode']
            local raw_msg = p['Message'] or p['message'] or ''
            local clean_msg = raw_msg:gsub('%z', ''):match('^%s*(.-)%s*$')

            if clean_msg and #clean_msg > 0 then
                local ls_mode = (mode == 5 and 'ls1') or (mode == 27 and 'ls2') or nil
                if ls_mode then
                    if clean_msg.strip_format then
                        clean_msg = clean_msg:strip_format()
                    end

                    local player = windower.ffxi.get_player()
                    local sender_name = player and player.name or 'Unknown'
                    local ls1_name, ls2_name = get_equipped_linkshells()
                    local server_name = get_server_name()
                    local active_ls = (ls_mode == 'ls2') and ls2_name or ls1_name

                    if settings.debug then
                        print_msg('Outgoing 0x0B5: mode=' .. tostring(mode) .. ' (' .. tostring(active_ls) .. ') msg=' .. clean_msg)
                    end

                    send_payload({
                        type = 'chat',
                        mode = ls_mode,
                        server = server_name,
                        linkshell = active_ls,
                        sender = sender_name,
                        message = clean_msg
                    })
                end
            end
        end)
        if not ok and settings.debug then
            print_error('0x0B5 error: ' .. tostring(err))
        end
    end
end)

-- In-game addon commands
windower.register_event('addon command', function(command, ...)
    local args = {...}
    command = (command or 'status'):lower()

    if command == 'status' then
        local player = windower.ffxi.get_player()
        local server_name = get_server_name()
        local ls1_name, ls2_name = get_equipped_linkshells()
        print_msg('Status:')
        print_msg('  Character: ' .. (player and player.name or 'Unknown') .. (server_name and (' on ' .. server_name) or ''))
        print_msg('  Linkshell 1 (/l): ' .. (ls1_name or 'None'))
        print_msg('  Linkshell 2 (/l2): ' .. (ls2_name or 'None'))
        
        local items = windower.ffxi.get_items()
        local eq = items and items.equipment or {}
        local dump = {}
        for k, v in pairs(eq) do
            if tostring(k):find('link') or tostring(k):find('ls') or tostring(k):find('pearl') or tostring(k):find('slot') then
                table.insert(dump, tostring(k) .. '=' .. tostring(v))
            end
        end
        if #dump == 0 then
            for k, v in pairs(eq) do
                table.insert(dump, tostring(k) .. '=' .. tostring(v))
            end
        end
        print_msg('  [EQ Dump] ' .. table.concat(dump, ', '))
        if eq.linkshell then
            local it1 = windower.ffxi.get_items(eq.linkshell_bag or 0, eq.linkshell)
            local ok, parsed = pcall(extdata.decode, it1)
            print_msg('  [linkshell item] id=' .. tostring(it1 and it1.id) .. ' name=' .. tostring(ok and parsed and parsed.name or 'nil'))
        end
        if eq.linkshell2 then
            local it2 = windower.ffxi.get_items(eq.linkshell2_bag or 0, eq.linkshell2)
            local ok, parsed = pcall(extdata.decode, it2)
            print_msg('  [linkshell2 item] id=' .. tostring(it2 and it2.id) .. ' name=' .. tostring(ok and parsed and parsed.name or 'nil'))
        end

        print_msg('  Hub Server: ' .. settings.tcp_host .. ':' .. settings.tcp_port)
        print_msg('  Connection: ' .. (is_connected and 'Connected' or 'Disconnected'))
        print_msg('  Private Tells: ' .. (settings.relay_tells and 'ON' or 'OFF'))
        print_msg('  GM Alerts: ' .. (settings.relay_gm and 'ON' or 'OFF'))
        if settings.relay_gm and settings.gm_unload_addons ~= '' then
            print_msg('  GM Unload Addons: ' .. settings.gm_unload_addons)
        end
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
        local server_name = get_server_name()
        local ls1_name, _ = get_equipped_linkshells()
        print_msg('Requesting pairing code for ' .. player.name .. '...')
        send_payload({
            type = 'request_pair_code',
            player = player.name,
            server = server_name,
            linkshell = ls1_name
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
    elseif command == 'tells' then
        local arg = args[1] and args[1]:lower()
        if arg == 'on' then settings.relay_tells = true
        elseif arg == 'off' then settings.relay_tells = false
        else settings.relay_tells = not settings.relay_tells end
        config.save(settings)
        print_msg('Private Tells relay: ' .. (settings.relay_tells and 'ON' or 'OFF'))
    elseif command == 'gm' then
        local arg = args[1] and args[1]:lower()
        if arg == 'on' then settings.relay_gm = true
        elseif arg == 'off' then settings.relay_gm = false
        else settings.relay_gm = not settings.relay_gm end
        config.save(settings)
        print_msg('GM Emergency Alerts: ' .. (settings.relay_gm and 'ON' or 'OFF'))
    elseif command == 'gmunload' then
        local list = args[1]
        if not list or list == '' then
            print_msg('Current GM unload addons: ' .. tostring(settings.gm_unload_addons))
            print_msg('Usage: //lsr gmunload <addon1,addon2,...>')
            return
        end
        settings.gm_unload_addons = list
        config.save(settings)
        print_msg('GM unload addons updated to: ' .. list)
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
        print_msg('  //lsr tells [on|off] - Toggle private tells relay to your Discord DM')
        print_msg('  //lsr gm [on|off] - Toggle high-priority GM alerts and safety protocol')
        print_msg('  //lsr gmunload <addons> - Comma-separated addons to unload on GM alert')
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
        local server_name = get_server_name()
        send_payload({
            type = 'hello',
            player = player_name,
            server = server_name
        })
    else
        try_connect()
    end
end)

windower.register_event('unload', function()
    disconnect()
end)
