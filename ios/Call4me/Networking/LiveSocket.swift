import Foundation

/// `WS /calls/{id}/live` client. Reconnects with backoff until stopped.
final class LiveSocket {
    private let callId: String
    private let settings: AppSettings
    private var task: URLSessionWebSocketTask?
    private var stopped = false
    private var retryDelay: TimeInterval = 1

    /// Called on the main queue for each message.
    var onEvent: ((LiveEvent) -> Void)?
    /// Called on the main queue when the connection drops or comes back.
    var onConnectionChange: ((Bool) -> Void)?

    init(callId: String, settings: AppSettings = .shared) {
        self.callId = callId
        self.settings = settings
    }

    func start() {
        stopped = false
        connect()
    }

    func stop() {
        stopped = true
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func socketURL() -> URL? {
        guard let base = settings.baseURL,
              var comps = URLComponents(url: base.appendingPathComponent("calls/\(callId)/live"),
                                        resolvingAgainstBaseURL: false)
        else { return nil }
        comps.scheme = (comps.scheme == "https") ? "wss" : "ws"
        // WebSockets carry the access code in the URL (the server accepts either form).
        if let token = settings.token {
            comps.queryItems = [URLQueryItem(name: "token", value: token)]
        }
        return comps.url
    }

    private func connect() {
        guard !stopped, let url = socketURL() else { return }
        let task = URLSession.shared.webSocketTask(with: url)
        self.task = task
        task.resume()
        receive(on: task)
    }

    private func receive(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self, task === self.task else { return }
            switch result {
            case .success(let message):
                self.retryDelay = 1
                DispatchQueue.main.async { self.onConnectionChange?(true) }
                self.handle(message)
                self.receive(on: task)
            case .failure:
                DispatchQueue.main.async { self.onConnectionChange?(false) }
                self.scheduleReconnect()
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        let data: Data
        switch message {
        case .string(let s): data = Data(s.utf8)
        case .data(let d): data = d
        @unknown default: return
        }
        guard let raw = try? JSONCoding.liveDecoder.decode(LiveMessage.self, from: data),
              let event = LiveEvent(raw) else { return }
        DispatchQueue.main.async { self.onEvent?(event) }
    }

    private func scheduleReconnect() {
        guard !stopped else { return }
        let delay = retryDelay
        retryDelay = min(retryDelay * 2, 15)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.connect()
        }
    }
}
