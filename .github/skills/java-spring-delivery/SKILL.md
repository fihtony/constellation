---
name: java-spring-delivery
description: >
  Java Spring Boot delivery playbook. Use when building REST APIs with Spring Boot,
  JPA entities, repositories, service layers, or testing with MockMvc.
user-invocable: false
---

# Java / Spring Boot Delivery

## When To Apply

- Building Spring Boot REST APIs (Spring Web MVC or WebFlux).
- JPA/Hibernate entities, repositories, and database migrations.
- Spring Security configuration, JWT authentication.
- Unit and integration tests with JUnit 5 and MockMvc.

## Project Structure

```
src/
  main/
    java/com/example/app/
      controller/    # @RestController classes
      service/       # @Service business logic
      repository/    # @Repository / JpaRepository interfaces
      model/         # @Entity classes
      dto/           # Request/response DTOs
      config/        # @Configuration classes
    resources/
      application.yml
      db/migration/  # Flyway SQL scripts (if used)
  test/
    java/com/example/app/
      controller/    # MockMvc integration tests
      service/       # Unit tests
```

## Controller Rules

- Annotate with `@RestController` and `@RequestMapping("/api/v1/resource")`.
- Use `@Valid` on `@RequestBody` parameters; define constraints in DTOs with `jakarta.validation`.
- Return `ResponseEntity<T>` with explicit HTTP status codes.
- Keep controllers thin — delegate all logic to service layer.

## Service & Repository

- Service classes annotated with `@Service`; inject repositories via constructor injection (not `@Autowired` field injection).
- Repository interfaces extend `JpaRepository<Entity, IdType>`; add custom queries with `@Query` JPQL or native SQL.
- Wrap mutating operations in `@Transactional`.

## Entity Rules

- Annotate with `@Entity`, `@Table(name = "...")`.
- Use `@Id` with `@GeneratedValue(strategy = GenerationType.IDENTITY)`.
- Avoid bidirectional `@OneToMany`/`@ManyToOne` unless necessary; use `FetchType.LAZY` for collections.
- Never expose `@Entity` classes directly in API responses — always map to DTOs.

## Build (Maven / Gradle)

- Maven: `mvn clean package -DskipTests` for packaging; `mvn test` for tests.
- Gradle: `./gradlew build` for packaging; `./gradlew test` for tests.
- Java version specified in `pom.xml` `<java.version>` or `build.gradle` `sourceCompatibility`.

## Quality Checklist

- Controller tests use `@SpringBootTest` with `MockMvc` or `@WebMvcTest`.
- All endpoints return appropriate HTTP status (200, 201, 400, 404, 500).
- Validation errors return 400 with field-level messages.
- No `System.out.println` — use SLF4J (`@Slf4j` + `log.info(...)`).
- Application starts with `mvn spring-boot:run` or `./gradlew bootRun`.
