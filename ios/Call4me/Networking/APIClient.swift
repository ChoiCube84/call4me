import Foundation

enum APIError: LocalizedError {
    case noServer
    case badAccessCode
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case .noServer: return "Server address is not set. Check Settings."
        case .badAccessCode: return "Wrong access code. Check Settings."
        case .http(let code, _): return "The server said no (code \(code)). Try again."
        }
    }
}

/// REST calls to the call4me backend (CLAUDE.md section 10).
/// Request bodies may contain private text (answers, typed lines): never log them.
struct APIClient {
    var settings: AppSettings = .shared
    var session: URLSession = .shared

    // MARK: Directory

    func searchCompanies(_ query: String) async throws -> [Company] {
        try await get("companies", query: [URLQueryItem(name: "q", value: query)])
    }

    // MARK: Calls

    func startCall(_ body: StartCallRequest) async throws -> CallSession {
        try await send("POST", "calls", body: body)
    }

    func getCall(_ id: String) async throws -> CallDetail {
        try await get("calls/\(id)")
    }

    func takeover(_ id: String) async throws { try await action("calls/\(id)/takeover") }
    func handback(_ id: String) async throws { try await action("calls/\(id)/handback") }
    func cancel(_ id: String) async throws { try await action("calls/\(id)/cancel") }

    func type(_ id: String, text: String) async throws {
        let _: Accepted = try await send("POST", "calls/\(id)/type", body: ["text": text])
    }

    /// Answer to the "Still on hold, keep waiting?" alert.
    func continueHold(_ id: String, keepWaiting: Bool) async throws {
        let _: Accepted = try await send("POST", "calls/\(id)/hold", body: ["keep_waiting": keepWaiting])
    }

    // MARK: ask_user

    func answer(requestId: String, answer: String) async throws {
        let _: Accepted = try await send("POST", "requests/\(requestId)/answer", body: ["answer": answer])
    }

    // MARK: Devices

    func registerDevice(_ body: DeviceRegistration) async throws {
        try await sendIgnoringBody("POST", "devices", body: body)
    }

    // MARK: Plumbing

    func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard let base = settings.baseURL,
              var comps = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)
        else { throw APIError.noServer }
        if !query.isEmpty { comps.queryItems = query }
        guard let url = comps.url else { throw APIError.noServer }
        return url
    }

    private func action(_ path: String) async throws {
        try await sendIgnoringBody("POST", path, body: Optional<String>.none)
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        var req = URLRequest(url: try url(path, query: query))
        req.httpMethod = "GET"
        authorize(&req)
        return try await decode(req)
    }

    private func send<T: Decodable, B: Encodable>(_ method: String, _ path: String, body: B?) async throws -> T {
        try await decode(try request(method, path, body: body))
    }

    private func sendIgnoringBody<B: Encodable>(_ method: String, _ path: String, body: B?) async throws {
        _ = try await perform(try request(method, path, body: body))
    }

    private func request<B: Encodable>(_ method: String, _ path: String, body: B?) throws -> URLRequest {
        var req = URLRequest(url: try url(path))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&req)
        if let body { req.httpBody = try JSONCoding.encoder.encode(body) }
        return req
    }

    private func authorize(_ req: inout URLRequest) {
        if let token = settings.token {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
    }

    private func decode<T: Decodable>(_ req: URLRequest) async throws -> T {
        let data = try await perform(req)
        return try JSONCoding.decoder.decode(T.self, from: data)
    }

    private func perform(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw APIError.badAccessCode }
        guard (200..<300).contains(code) else {
            throw APIError.http(code, String(decoding: data, as: UTF8.self))
        }
        return data
    }
}

struct DeviceRegistration: Encodable {
    var userId: String
    var apnsToken: String?
    var liveActivityToken: String?
}
