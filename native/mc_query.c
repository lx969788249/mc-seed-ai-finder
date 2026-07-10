#include "../vendor/cubiomes/finders.h"
#include "../vendor/cubiomes/util.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *id;
    int value;
} NameMap;

typedef struct {
    int x;
    int z;
    double distance;
} SearchResult;

typedef struct {
    const char *kind;
    const char *id;
    int radius;
    int limit;
} ComboTarget;

static int biome_matches_area_target(int biome_id, int target_biome, const char *target_id);
static int surface_biome_at(
    const Generator *g,
    const SurfaceNoise *sn,
    int x,
    int z,
    float *surface_height,
    int *sample_y
);

static NameMap structures[] = {
    {"village", Village},
    {"witch_hut", Swamp_Hut},
    {"pillager_outpost", Outpost},
    {"desert_pyramid", Desert_Pyramid},
    {"jungle_pyramid", Jungle_Pyramid},
    {"jungle_temple", Jungle_Temple},
    {"igloo", Igloo},
    {"ocean_monument", Monument},
    {"woodland_mansion", Mansion},
    {"ruined_portal", Ruined_Portal},
    {"ancient_city", Ancient_City},
    {"trial_chambers", Trial_Chambers},
    {"shipwreck", Shipwreck},
    {"nether_fortress", Fortress},
    {"bastion_remnant", Bastion},
    {"end_city", End_City},
    {NULL, 0},
};

static NameMap biomes[] = {
    {"plains", plains},
    {"sunflower_plains", sunflower_plains},
    {"cherry_grove", cherry_grove},
    {"swamp", swamp},
    {"mangrove_swamp", mangrove_swamp},
    {"forest", forest},
    {"flower_forest", flower_forest},
    {"dark_forest", dark_forest},
    {"desert", desert},
    {"jungle", jungle},
    {"badlands", badlands},
    {"savanna", savanna},
    {"snowy_plains", snowy_plains},
    {"meadow", meadow},
    {"grove", grove},
    {"snowy_slopes", snowy_slopes},
    {"jagged_peaks", jagged_peaks},
    {"frozen_peaks", frozen_peaks},
    {"stony_peaks", stony_peaks},
    {"mushroom_fields", mushroom_fields},
    {"ocean", ocean},
    {"warm_ocean", warm_ocean},
    {"lukewarm_ocean", lukewarm_ocean},
    {"deep_ocean", deep_ocean},
    {"river", river},
    {"beach", beach},
    {NULL, 0},
};

static int lookup(NameMap *map, const char *id, int *out)
{
    for (int i = 0; map[i].id; i++) {
        if (!strcmp(map[i].id, id)) {
            *out = map[i].value;
            return 1;
        }
    }
    return 0;
}

static int dim_for_structure(int st)
{
    if (st == Fortress || st == Bastion) return DIM_NETHER;
    if (st == End_City) return DIM_END;
    return DIM_OVERWORLD;
}

static double dist2(int x, int z, int cx, int cz)
{
    double dx = (double)x - (double)cx;
    double dz = (double)z - (double)cz;
    return dx * dx + dz * dz;
}

static void insert_best(Pos *best, double *bestd, int *found, int limit, int x, int z, double d)
{
    int slot = -1;
    for (int i = 0; i < limit; i++) {
        if (d < bestd[i]) {
            slot = i;
            break;
        }
    }
    if (slot < 0) return;
    for (int i = limit - 1; i > slot; i--) {
        bestd[i] = bestd[i - 1];
        best[i] = best[i - 1];
    }
    bestd[slot] = d;
    best[slot].x = x;
    best[slot].z = z;
    if (*found < limit) (*found)++;
}

static int too_close(Pos *best, int found, int x, int z, int minsep)
{
    double min2 = (double)minsep * minsep;
    for (int i = 0; i < found; i++) {
        if (dist2(best[i].x, best[i].z, x, z) < min2) return 1;
    }
    return 0;
}

static void unsupported(const char *message)
{
    printf("{\"ok\":false,\"error\":\"%s\",\"results\":[]}\n", message);
}

static void print_result_array(SearchResult *results, int found)
{
    printf("[");
    for (int i = 0; i < found; i++) {
        if (i) printf(",");
        printf("{\"x\":%d,\"z\":%d,\"distance\":%.1f}", results[i].x, results[i].z, results[i].distance);
    }
    printf("]");
}

static int collect_biome(int mc, int64_t seed, const char *id, int cx, int cz, int radius, int limit, SearchResult **out, int *out_count, const char **error)
{
    int biome = 0;
    *out = NULL;
    *out_count = 0;
    *error = NULL;
    if (!lookup(biomes, id, &biome)) {
        *error = "unsupported_biome";
        return 2;
    }

    Generator g;
    setupGenerator(&g, mc, 0);
    applySeed(&g, DIM_OVERWORLD, (uint64_t)seed);
    SurfaceNoise sn;
    initSurfaceNoise(&sn, DIM_OVERWORLD, (uint64_t)seed);

    int step = 64;
    double *bestd = (double *)malloc((size_t)limit * sizeof(double));
    Pos *best = (Pos *)malloc((size_t)limit * sizeof(Pos));
    if (!bestd || !best) {
        free(bestd);
        free(best);
        *error = "allocation_failed";
        return 2;
    }
    int found = 0;
    for (int i = 0; i < limit; i++) bestd[i] = 1.0 / 0.0;

    int maxRing = radius / step + 2;
    for (int ring = 0; ring <= maxRing; ring++) {
        for (int dz = -ring; dz <= ring; dz++) {
            for (int dx = -ring; dx <= ring; dx++) {
                if (ring > 0 && abs(dx) != ring && abs(dz) != ring) continue;
                int x = cx + dx * step;
                int z = cz + dz * step;
            if (dist2(x, z, cx, cz) > (double)radius * radius) continue;
            int got = surface_biome_at(&g, &sn, x, z, NULL, NULL);
            if (!biome_matches_area_target(got, biome, id)) continue;
            double d = dist2(x, z, cx, cz);
                if (too_close(best, found, x, z, 512)) continue;
                insert_best(best, bestd, &found, limit, x, z, d);
            }
        }
        if (found >= limit && (double)ring * step > sqrt(bestd[limit - 1]) + step * 2) {
            break;
        }
    }

    SearchResult *results = (SearchResult *)malloc((size_t)found * sizeof(SearchResult));
    if (found > 0 && !results) {
        free(bestd);
        free(best);
        *error = "allocation_failed";
        return 2;
    }
    for (int i = 0; i < found; i++) {
        results[i].x = best[i].x;
        results[i].z = best[i].z;
        results[i].distance = sqrt(bestd[i]);
    }
    free(bestd);
    free(best);
    *out = results;
    *out_count = found;
    return 0;
}

static int search_biome(int mc, int64_t seed, const char *id, int cx, int cz, int radius, int limit)
{
    SearchResult *results = NULL;
    int found = 0;
    const char *error = NULL;
    int code = collect_biome(mc, seed, id, cx, cz, radius, limit, &results, &found, &error);
    if (code) {
        unsupported(error ? error : "biome_query_failed");
        return code;
    }
    printf("{\"ok\":true,\"backend\":\"cubiomes\",\"mode\":\"exact\",\"results\":");
    print_result_array(results, found);
    printf("}\n");
    free(results);
    return 0;
}

static int collect_biome_near(Generator *g, const SurfaceNoise *sn, const char *id, int cx, int cz, int radius, int limit, SearchResult **out, int *out_count, const char **error)
{
    int biome = 0;
    *out = NULL;
    *out_count = 0;
    *error = NULL;
    if (!lookup(biomes, id, &biome)) {
        *error = "unsupported_biome";
        return 2;
    }

    if (limit < 1) limit = 1;
    int step = radius <= 128 ? 16 : (radius <= 512 ? 32 : 64);
    if (step < 1) step = 1;
    int maxRing = radius / step + 2;

    double *bestd = (double *)malloc((size_t)limit * sizeof(double));
    Pos *best = (Pos *)malloc((size_t)limit * sizeof(Pos));
    if (!bestd || !best) {
        free(bestd);
        free(best);
        *error = "allocation_failed";
        return 2;
    }
    int found = 0;
    for (int i = 0; i < limit; i++) bestd[i] = 1.0 / 0.0;

    for (int ring = 0; ring <= maxRing; ring++) {
        for (int dz = -ring; dz <= ring; dz++) {
            for (int dx = -ring; dx <= ring; dx++) {
                if (ring > 0 && abs(dx) != ring && abs(dz) != ring) continue;
                int x = cx + dx * step;
                int z = cz + dz * step;
                if (dist2(x, z, cx, cz) > (double)radius * radius) continue;
                int got = surface_biome_at(g, sn, x, z, NULL, NULL);
                if (!biome_matches_area_target(got, biome, id)) continue;
                double d = dist2(x, z, cx, cz);
                if (too_close(best, found, x, z, step)) continue;
                insert_best(best, bestd, &found, limit, x, z, d);
            }
        }
        if (found >= limit && (double)ring * step > sqrt(bestd[limit - 1]) + step * 2) {
            break;
        }
    }

    SearchResult *results = (SearchResult *)malloc((size_t)found * sizeof(SearchResult));
    if (found > 0 && !results) {
        free(bestd);
        free(best);
        *error = "allocation_failed";
        return 2;
    }
    for (int i = 0; i < found; i++) {
        results[i].x = best[i].x;
        results[i].z = best[i].z;
        results[i].distance = sqrt(bestd[i]);
    }
    free(bestd);
    free(best);
    *out = results;
    *out_count = found;
    return 0;
}

static int collect_structure(int mc, int64_t seed, const char *id, int cx, int cz, int radius, int limit, SearchResult **out, int *out_count, const char **error)
{
    int st = 0;
    *out = NULL;
    *out_count = 0;
    *error = NULL;
    if (!lookup(structures, id, &st)) {
        *error = "unsupported_structure";
        return 2;
    }

    StructureConfig conf;
    if (!getStructureConfig(st, mc, &conf)) {
        *error = "structure_not_supported_in_version";
        return 2;
    }

    int dim = dim_for_structure(st);
    Generator g;
    setupGenerator(&g, mc, 0);
    applySeed(&g, dim, (uint64_t)seed);

    int regBlocks = conf.regionSize * 16;
    int centerRegX = floor((double)cx / regBlocks);
    int centerRegZ = floor((double)cz / regBlocks);
    int maxRing = radius / regBlocks + 3;

    double *bestd = (double *)malloc((size_t)limit * sizeof(double));
    Pos *best = (Pos *)malloc((size_t)limit * sizeof(Pos));
    if (!bestd || !best) {
        free(bestd);
        free(best);
        *error = "allocation_failed";
        return 2;
    }
    int found = 0;
    for (int i = 0; i < limit; i++) bestd[i] = 1.0 / 0.0;

    for (int ring = 0; ring <= maxRing; ring++) {
        for (int dz = -ring; dz <= ring; dz++) {
            for (int dx = -ring; dx <= ring; dx++) {
                if (ring > 0 && abs(dx) != ring && abs(dz) != ring) continue;
                int rx = centerRegX + dx;
                int rz = centerRegZ + dz;
            Pos p;
            if (!getStructurePos(st, mc, (uint64_t)seed, rx, rz, &p)) continue;
            double d = dist2(p.x, p.z, cx, cz);
            if (d > (double)radius * radius) continue;
            if (!isViableStructurePos(st, &g, p.x, p.z, 0)) continue;
                insert_best(best, bestd, &found, limit, p.x, p.z, d);
            }
        }
        if (found >= limit && (double)ring * regBlocks > sqrt(bestd[limit - 1]) + regBlocks * 2) {
            break;
        }
    }

    SearchResult *results = (SearchResult *)malloc((size_t)found * sizeof(SearchResult));
    if (found > 0 && !results) {
        free(bestd);
        free(best);
        *error = "allocation_failed";
        return 2;
    }
    for (int i = 0; i < found; i++) {
        results[i].x = best[i].x;
        results[i].z = best[i].z;
        results[i].distance = sqrt(bestd[i]);
    }
    free(bestd);
    free(best);
    *out = results;
    *out_count = found;
    return 0;
}

static int search_structure(int mc, int64_t seed, const char *id, int cx, int cz, int radius, int limit)
{
    SearchResult *results = NULL;
    int found = 0;
    const char *error = NULL;
    int code = collect_structure(mc, seed, id, cx, cz, radius, limit, &results, &found, &error);
    if (code) {
        unsupported(error ? error : "structure_query_failed");
        return code;
    }
    printf("{\"ok\":true,\"backend\":\"cubiomes\",\"mode\":\"exact\",\"results\":");
    print_result_array(results, found);
    printf("}\n");
    free(results);
    return 0;
}

static int find_biome_slot(int *ids, int count, int id)
{
    for (int i = 0; i < count; i++) {
        if (ids[i] == id) return i;
    }
    return -1;
}

static int floor_div_int(int value, int divisor)
{
    int quotient = value / divisor;
    int remainder = value % divisor;
    if (remainder < 0) quotient--;
    return quotient;
}

static int is_underground_biome_id(int biome_id)
{
    return biome_id == lush_caves || biome_id == dripstone_caves || biome_id == deep_dark;
}

static int surface_biome_at(
    const Generator *g,
    const SurfaceNoise *sn,
    int x,
    int z,
    float *surface_height,
    int *sample_y
)
{
    float height = 63.0f;
    int scaled_x = floor_div_int(x, 4);
    int scaled_z = floor_div_int(z, 4);
    if (mapApproxHeight(&height, NULL, g, sn, scaled_x, scaled_z, 1, 1) != 0 || !isfinite(height)) {
        height = 63.0f;
    }

    int y = (int)lroundf(height) + 8;
    if (y < 64) y = 64;
    if (y > 319) y = 319;
    int biome_id = getBiomeAt(g, 1, x, y, z);
    while (is_underground_biome_id(biome_id) && y < 319) {
        y += 16;
        if (y > 319) y = 319;
        biome_id = getBiomeAt(g, 1, x, y, z);
    }

    if (surface_height) *surface_height = height;
    if (sample_y) *sample_y = y;
    return biome_id;
}

static int biome_matches_area_target(int biome_id, int target_biome, const char *target_id)
{
    if (!strcmp(target_id, "ocean")) return isOceanic(biome_id);
    if (!strcmp(target_id, "mushroom_fields")) {
        return biome_id == mushroom_fields || biome_id == mushroom_field_shore;
    }
    return biome_id == target_biome;
}

static int measure_biome_area(int mc, int64_t seed, const char *id, int cx, int cz, int radius, int step)
{
    int target_biome = 0;
    if (!lookup(biomes, id, &target_biome)) {
        unsupported("unsupported_biome");
        return 2;
    }
    if (step != 1 && step != 4 && step != 16 && step != 64 && step != 256) {
        unsupported("unsupported_area_step");
        return 2;
    }
    if (radius < step * 4) radius = step * 4;
    int half = radius / step;
    int size = half * 2 + 1;
    if (size < 3 || size > 8193) {
        unsupported("area_grid_too_large");
        return 2;
    }

    Generator g;
    setupGenerator(&g, mc, 0);
    applySeed(&g, DIM_OVERWORLD, (uint64_t)seed);

    int center_cell_x = floor_div_int(cx, step);
    int center_cell_z = floor_div_int(cz, step);
    Range range = {
        step,
        center_cell_x - half,
        center_cell_z - half,
        size,
        size,
        step == 1 ? 63 : 15,
        1,
    };
    int *cells = allocCache(&g, range);
    if (!cells) {
        unsupported("allocation_failed");
        return 2;
    }
    if (genBiomes(&g, cells, range)) {
        free(cells);
        unsupported("biome_area_generation_failed");
        return 2;
    }

    size_t total = (size_t)size * (size_t)size;
    int seed_index = -1;
    double best_seed_distance = 1.0 / 0.0;
    for (int row = 0; row < size; row++) {
        for (int col = 0; col < size; col++) {
            int index = row * size + col;
            if (!biome_matches_area_target(cells[index], target_biome, id)) continue;
            double dx = (double)(col - half);
            double dz = (double)(row - half);
            double distance = dx * dx + dz * dz;
            if (distance < best_seed_distance) {
                best_seed_distance = distance;
                seed_index = index;
            }
        }
    }
    if (seed_index < 0) {
        free(cells);
        unsupported("target_biome_not_found_near_sample");
        return 2;
    }

    unsigned char *visited = (unsigned char *)calloc(total, 1);
    int *queue = (int *)malloc(total * sizeof(int));
    if (!visited || !queue) {
        free(visited);
        free(queue);
        free(cells);
        unsupported("allocation_failed");
        return 2;
    }

    size_t head = 0;
    size_t tail = 0;
    queue[tail++] = seed_index;
    visited[seed_index] = 1;
    int min_col = seed_index % size;
    int max_col = min_col;
    int min_row = seed_index / size;
    int max_row = min_row;
    int touches_boundary = 0;
    int64_t cell_count = 0;
    int64_t sum_col = 0;
    int64_t sum_row = 0;
    int64_t perimeter = 0;
    const int dc[4] = {-1, 1, 0, 0};
    const int dr[4] = {0, 0, -1, 1};

    while (head < tail) {
        int index = queue[head++];
        int col = index % size;
        int row = index / size;
        cell_count++;
        sum_col += col;
        sum_row += row;
        if (col < min_col) min_col = col;
        if (col > max_col) max_col = col;
        if (row < min_row) min_row = row;
        if (row > max_row) max_row = row;
        if (col == 0 || row == 0 || col == size - 1 || row == size - 1) touches_boundary = 1;

        for (int direction = 0; direction < 4; direction++) {
            int next_col = col + dc[direction];
            int next_row = row + dr[direction];
            if (next_col < 0 || next_row < 0 || next_col >= size || next_row >= size) {
                perimeter += step;
                continue;
            }
            int next = next_row * size + next_col;
            if (!biome_matches_area_target(cells[next], target_biome, id)) {
                perimeter += step;
                continue;
            }
            if (!visited[next]) {
                visited[next] = 1;
                queue[tail++] = next;
            }
        }
    }

    int64_t area = cell_count * (int64_t)step * (int64_t)step;
    double average_col = (double)sum_col / (double)cell_count;
    double average_row = (double)sum_row / (double)cell_count;
    int representative_index = seed_index;
    double representative_distance = 1.0 / 0.0;
    for (size_t index = 0; index < total; index++) {
        if (!visited[index]) continue;
        int col = (int)(index % (size_t)size);
        int row = (int)(index / (size_t)size);
        double dx = col - average_col;
        double dz = row - average_row;
        double distance = dx * dx + dz * dz;
        if (distance < representative_distance) {
            representative_distance = distance;
            representative_index = (int)index;
        }
    }
    int representative_col = representative_index % size;
    int representative_row = representative_index / size;
    int64_t center_x = ((int64_t)range.x + representative_col) * step + step / 2;
    int64_t center_z = ((int64_t)range.z + representative_row) * step + step / 2;
    int min_x = (range.x + min_col) * step;
    int max_x = (range.x + max_col + 1) * step - 1;
    int min_z = (range.z + min_row) * step;
    int max_z = (range.z + max_row + 1) * step - 1;

    printf(
        "{\"ok\":true,\"backend\":\"cubiomes\",\"kind\":\"biome_area\",\"id\":\"%s\","
        "\"sample\":{\"x\":%d,\"z\":%d},\"center\":{\"x\":%" PRId64 ",\"z\":%" PRId64 "},"
        "\"area\":%" PRId64 ",\"cell_count\":%" PRId64 ",\"step\":%d,\"radius\":%d,"
        "\"perimeter\":%" PRId64 ",\"bounds\":{\"min_x\":%d,\"max_x\":%d,\"min_z\":%d,\"max_z\":%d},"
        "\"width\":%d,\"height\":%d,\"closed\":%s,\"truncated\":%s,\"sample_y\":63}\n",
        id,
        cx,
        cz,
        center_x,
        center_z,
        area,
        cell_count,
        step,
        radius,
        perimeter,
        min_x,
        max_x,
        min_z,
        max_z,
        max_x - min_x + 1,
        max_z - min_z + 1,
        touches_boundary ? "false" : "true",
        touches_boundary ? "true" : "false"
    );

    free(visited);
    free(queue);
    free(cells);
    return 0;
}

static uint64_t sample_rng_next(uint64_t *state)
{
    uint64_t value = *state;
    value ^= value >> 12;
    value ^= value << 25;
    value ^= value >> 27;
    *state = value;
    return value * UINT64_C(2685821657736338717);
}

static int sample_biome_candidates(int mc, int64_t seed, const char *id, int cx, int cz, int radius, int samples, int limit)
{
    int target_biome = 0;
    if (!lookup(biomes, id, &target_biome)) {
        unsupported("unsupported_biome");
        return 2;
    }
    if (radius < 1) radius = 1;
    if (samples < 1) samples = 1;
    if (samples > 1000000) samples = 1000000;
    if (limit < 1) limit = 1;
    if (limit > 512) limit = 512;

    Generator g;
    setupGenerator(&g, mc, 0);
    applySeed(&g, DIM_OVERWORLD, (uint64_t)seed);
    SearchResult *results = (SearchResult *)calloc((size_t)limit, sizeof(SearchResult));
    Pos *accepted = (Pos *)calloc((size_t)limit, sizeof(Pos));
    if (!results || !accepted) {
        free(results);
        free(accepted);
        unsupported("allocation_failed");
        return 2;
    }

    uint64_t state = (uint64_t)seed ^ UINT64_C(0x9e3779b97f4a7c15);
    for (const char *cursor = id; *cursor; cursor++) state = state * 131 + (unsigned char)*cursor;
    if (!state) state = UINT64_C(0x6a09e667f3bcc909);
    uint64_t span = (uint64_t)radius * 2 + 1;
    int found = 0;
    int minimum_separation = !strcmp(id, "ocean") ? 8192 : 2048;
    double radius_squared = (double)radius * (double)radius;

    for (int checked = 0; checked < samples; checked++) {
        int x;
        int z;
        do {
            int64_t offset_x = (int64_t)(sample_rng_next(&state) % span) - radius;
            int64_t offset_z = (int64_t)(sample_rng_next(&state) % span) - radius;
            int64_t raw_x = (int64_t)cx + offset_x;
            int64_t raw_z = (int64_t)cz + offset_z;
            if (raw_x < -30000000) raw_x = -30000000;
            if (raw_x > 30000000) raw_x = 30000000;
            if (raw_z < -30000000) raw_z = -30000000;
            if (raw_z > 30000000) raw_z = 30000000;
            x = (int)raw_x;
            z = (int)raw_z;
        } while (dist2(x, z, cx, cz) > radius_squared);

        int biome_id = getBiomeAt(&g, 1, x, 63, z);
        if (!biome_matches_area_target(biome_id, target_biome, id)) continue;
        if (too_close(accepted, found, x, z, minimum_separation)) continue;
        if (found < limit) {
            accepted[found].x = x;
            accepted[found].z = z;
            results[found].x = x;
            results[found].z = z;
            results[found].distance = sqrt(dist2(x, z, cx, cz));
            found++;
        }
    }

    printf("{\"ok\":true,\"backend\":\"cubiomes\",\"kind\":\"biome_samples\",\"id\":\"%s\",\"samples_checked\":%d,\"radius\":%d,\"results\":", id, samples, radius);
    print_result_array(results, found);
    printf("}\n");
    free(results);
    free(accepted);
    return 0;
}

static int render_map(int mc, int64_t seed, int cx, int cz, int radius, int size)
{
    if (size < 16) size = 16;
    if (size > 256) size = 256;
    if (radius < 1) radius = 1;

    Generator g;
    setupGenerator(&g, mc, 0);
    applySeed(&g, DIM_OVERWORLD, (uint64_t)seed);
    SurfaceNoise sn;
    initSurfaceNoise(&sn, DIM_OVERWORLD, (uint64_t)seed);

    int step = (2 * radius) / (size - 1);
    if (step < 1) step = 1;
    int x0 = cx - radius;
    int z0 = cz - radius;
    int unique[512];
    int unique_count = 0;
    int *cells = (int *)calloc((size_t)size * (size_t)size, sizeof(int));
    int *heights = (int *)calloc((size_t)size * (size_t)size, sizeof(int));
    if (!cells || !heights) {
        free(cells);
        free(heights);
        unsupported("allocation_failed");
        return 2;
    }

    int height_min = 319;
    int height_max = -64;

    for (int row = 0; row < size; row++) {
        int z = z0 + row * step;
        for (int col = 0; col < size; col++) {
            int x = x0 + col * step;
            float surface_height = 63.0f;
            int id = surface_biome_at(&g, &sn, x, z, &surface_height, NULL);
            int height = (int)lroundf(surface_height);
            if (height < -64) height = -64;
            if (height > 319) height = 319;
            heights[row * size + col] = height;
            if (height < height_min) height_min = height;
            if (height > height_max) height_max = height;
            int slot = find_biome_slot(unique, unique_count, id);
            if (slot < 0) {
                if (unique_count >= 512) slot = 0;
                else {
                    slot = unique_count;
                    unique[unique_count++] = id;
                }
            }
            cells[row * size + col] = slot;
        }
    }

    printf("{\"ok\":true,\"backend\":\"cubiomes\",\"mode\":\"exact\",\"projection\":\"surface\",\"height_mode\":\"approximate\",\"height_min\":%d,\"height_max\":%d,\"x0\":%d,\"z0\":%d,\"step\":%d,\"size\":%d,\"biomes\":[", height_min, height_max, x0, z0, step, size);
    for (int i = 0; i < unique_count; i++) {
        if (i) printf(",");
        const char *name = biome2str(mc, unique[i]);
        printf("{\"id\":%d,\"name\":\"%s\"}", unique[i], name ? name : "unknown");
    }
    printf("],\"cells\":[");
    for (int i = 0; i < size * size; i++) {
        if (i) printf(",");
        printf("%d", cells[i]);
    }
    printf("],\"heights\":[");
    for (int i = 0; i < size * size; i++) {
        if (i) printf(",");
        printf("%d", heights[i]);
    }
    printf("]}\n");
    free(cells);
    free(heights);
    return 0;
}

static int biome_at(int mc, int64_t seed, int x, int z)
{
    Generator g;
    setupGenerator(&g, mc, 0);
    applySeed(&g, DIM_OVERWORLD, (uint64_t)seed);
    SurfaceNoise sn;
    initSurfaceNoise(&sn, DIM_OVERWORLD, (uint64_t)seed);

    float surface_height = 63.0f;
    int sample_y = 64;
    int id = surface_biome_at(&g, &sn, x, z, &surface_height, &sample_y);
    const char *name = biome2str(mc, id);
    printf("{\"ok\":true,\"backend\":\"cubiomes\",\"projection\":\"surface\",\"surface_height\":%.1f,\"sample_y\":%d,\"biome\":{\"id\":%d,\"name\":\"%s\"}}\n", surface_height, sample_y, id, name ? name : "unknown");
    return 0;
}

static int print_biome_at_item(int mc, Generator *g, const SurfaceNoise *sn, const char *id, int x, int z)
{
    float surface_height = 63.0f;
    int sample_y = 64;
    int biome_id = surface_biome_at(g, sn, x, z, &surface_height, &sample_y);
    const char *name = biome2str(mc, biome_id);
    printf("{\"ok\":true,\"kind\":\"biome_at\",\"id\":\"%s\",\"center_x\":%d,\"center_z\":%d,\"projection\":\"surface\",\"surface_height\":%.1f,\"sample_y\":%d,\"biome\":{\"id\":%d,\"name\":\"%s\"},\"results\":[]}",
        id, x, z, surface_height, sample_y, biome_id, name ? name : "unknown");
    return 0;
}

static int batch_query(int mc, int64_t seed, int argc, char **argv)
{
    if (argc < 5) {
        unsupported("usage: mc_query batch version seed count kind id center_x center_z radius limit ...");
        return 2;
    }
    int count = atoi(argv[4]);
    if (count < 1 || count > 512 || argc != 5 + count * 6) {
        unsupported("usage: mc_query batch version seed count kind id center_x center_z radius limit ...");
        return 2;
    }

    Generator biome_g;
    setupGenerator(&biome_g, mc, 0);
    applySeed(&biome_g, DIM_OVERWORLD, (uint64_t)seed);
    SurfaceNoise biome_sn;
    initSurfaceNoise(&biome_sn, DIM_OVERWORLD, (uint64_t)seed);

    printf("{\"ok\":true,\"backend\":\"cubiomes\",\"mode\":\"exact\",\"results\":[");
    for (int i = 0; i < count; i++) {
        int base = 5 + i * 6;
        const char *kind = argv[base];
        const char *id = argv[base + 1];
        int cx = atoi(argv[base + 2]);
        int cz = atoi(argv[base + 3]);
        int radius = atoi(argv[base + 4]);
        int limit = atoi(argv[base + 5]);
        if (radius < 1) radius = 1;
        if (limit < 1) limit = 1;
        if (limit > 32768) limit = 32768;

        if (i) printf(",");

        if (!strcmp(kind, "biome_at")) {
            print_biome_at_item(mc, &biome_g, &biome_sn, id, cx, cz);
            continue;
        }

        SearchResult *results = NULL;
        int found = 0;
        const char *error = NULL;
        int code = 2;
        if (!strcmp(kind, "structure")) {
            code = collect_structure(mc, seed, id, cx, cz, radius, limit, &results, &found, &error);
        } else if (!strcmp(kind, "biome")) {
            code = collect_biome(mc, seed, id, cx, cz, radius, limit, &results, &found, &error);
        } else if (!strcmp(kind, "biome_near")) {
            code = collect_biome_near(&biome_g, &biome_sn, id, cx, cz, radius, limit, &results, &found, &error);
        } else {
            error = "unsupported_kind";
        }

        printf("{\"ok\":%s,\"kind\":\"%s\",\"id\":\"%s\",\"center_x\":%d,\"center_z\":%d,\"radius\":%d,\"limit\":%d",
            code ? "false" : "true", kind, id, cx, cz, radius, limit);
        if (code) {
            printf(",\"error\":\"%s\",\"results\":[]}", error ? error : "query_failed");
        } else {
            printf(",\"results\":");
            print_result_array(results, found);
            printf("}");
        }
        free(results);
    }
    printf("]}\n");
    return 0;
}

static int anchor_combo_query(int mc, int64_t seed, int argc, char **argv)
{
    if (argc < 8) {
        unsupported("usage: mc_query anchor_combo version seed target_count anchor_count kind id radius limit ... anchor_x anchor_z ...");
        return 2;
    }
    int target_count = atoi(argv[4]);
    int anchor_count = atoi(argv[5]);
    if (target_count < 1 || target_count > 32 || anchor_count < 1 || anchor_count > 512) {
        unsupported("anchor_combo_count_out_of_range");
        return 2;
    }
    int expected = 6 + target_count * 4 + anchor_count * 2;
    if (argc != expected) {
        unsupported("usage: mc_query anchor_combo version seed target_count anchor_count kind id radius limit ... anchor_x anchor_z ...");
        return 2;
    }

    ComboTarget targets[32];
    int pos = 6;
    for (int i = 0; i < target_count; i++) {
        targets[i].kind = argv[pos++];
        targets[i].id = argv[pos++];
        targets[i].radius = atoi(argv[pos++]);
        targets[i].limit = atoi(argv[pos++]);
        if (targets[i].radius < 1) targets[i].radius = 1;
        if (targets[i].limit < 1) targets[i].limit = 1;
        if (targets[i].limit > 32768) targets[i].limit = 32768;
    }

    Generator biome_g;
    setupGenerator(&biome_g, mc, 0);
    applySeed(&biome_g, DIM_OVERWORLD, (uint64_t)seed);
    SurfaceNoise biome_sn;
    initSurfaceNoise(&biome_sn, DIM_OVERWORLD, (uint64_t)seed);

    printf("{\"ok\":true,\"backend\":\"cubiomes\",\"mode\":\"exact\",\"results\":[");
    for (int anchor_idx = 0; anchor_idx < anchor_count; anchor_idx++) {
        int ax = atoi(argv[pos++]);
        int az = atoi(argv[pos++]);
        int complete = 1;
        if (anchor_idx) printf(",");
        printf("{\"anchor_x\":%d,\"anchor_z\":%d,\"targets\":[", ax, az);

        int printed_targets = 0;
        for (int target_idx = 0; target_idx < target_count; target_idx++) {
            SearchResult *results = NULL;
            int found = 0;
            const char *error = NULL;
            int code = 2;
            ComboTarget *target = &targets[target_idx];

            if (!strcmp(target->kind, "structure")) {
                code = collect_structure(mc, seed, target->id, ax, az, target->radius, target->limit, &results, &found, &error);
            } else if (!strcmp(target->kind, "biome")) {
                if (target->radius <= 4096) {
                    code = collect_biome_near(&biome_g, &biome_sn, target->id, ax, az, target->radius, target->limit, &results, &found, &error);
                } else {
                    code = collect_biome(mc, seed, target->id, ax, az, target->radius, target->limit, &results, &found, &error);
                }
            } else {
                error = "unsupported_kind";
            }

            if (printed_targets) printf(",");
            printed_targets++;
            printf("{\"ok\":%s,\"kind\":\"%s\",\"id\":\"%s\",\"radius\":%d,\"limit\":%d",
                code ? "false" : "true", target->kind, target->id, target->radius, target->limit);
            if (code) {
                printf(",\"error\":\"%s\",\"results\":[]}", error ? error : "query_failed");
                complete = 0;
                free(results);
                break;
            }
            printf(",\"results\":");
            print_result_array(results, found);
            printf("}");
            if (found <= 0) {
                complete = 0;
                free(results);
                break;
            }
            free(results);
        }
        printf("],\"complete\":%s}", complete ? "true" : "false");
    }
    printf("]}\n");
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        unsupported("usage: mc_query structure|biome|map|batch ...");
        return 2;
    }

    const char *kind = argv[1];

    if (!strcmp(kind, "anchor_combo")) {
        if (argc < 6) {
            unsupported("usage: mc_query anchor_combo version seed target_count anchor_count kind id radius limit ... anchor_x anchor_z ...");
            return 2;
        }
        int mc = str2mc(argv[2]);
        if (mc == MC_UNDEF) {
            unsupported("version_not_supported_by_cubiomes");
            return 2;
        }
        int64_t seed = strtoll(argv[3], NULL, 10);
        return anchor_combo_query(mc, seed, argc, argv);
    }

    if (!strcmp(kind, "batch")) {
        if (argc < 5) {
            unsupported("usage: mc_query batch version seed count kind id center_x center_z radius limit ...");
            return 2;
        }
        int mc = str2mc(argv[2]);
        if (mc == MC_UNDEF) {
            unsupported("version_not_supported_by_cubiomes");
            return 2;
        }
        int64_t seed = strtoll(argv[3], NULL, 10);
        return batch_query(mc, seed, argc, argv);
    }

    if (!strcmp(kind, "biome_at")) {
        if (argc != 6) {
            unsupported("usage: mc_query biome_at version seed x z");
            return 2;
        }
        int mc = str2mc(argv[2]);
        if (mc == MC_UNDEF) {
            unsupported("version_not_supported_by_cubiomes");
            return 2;
        }
        int64_t seed = strtoll(argv[3], NULL, 10);
        int x = atoi(argv[4]);
        int z = atoi(argv[5]);
        return biome_at(mc, seed, x, z);
    }

    if (!strcmp(kind, "map")) {
        if (argc != 8) {
            unsupported("usage: mc_query map version seed center_x center_z radius size");
            return 2;
        }
        int mc = str2mc(argv[2]);
        if (mc == MC_UNDEF) {
            unsupported("version_not_supported_by_cubiomes");
            return 2;
        }
        int64_t seed = strtoll(argv[3], NULL, 10);
        int cx = atoi(argv[4]);
        int cz = atoi(argv[5]);
        int radius = atoi(argv[6]);
        int size = atoi(argv[7]);
        return render_map(mc, seed, cx, cz, radius, size);
    }

    if (!strcmp(kind, "biome_area")) {
        if (argc != 9) {
            unsupported("usage: mc_query biome_area version seed id x z radius step");
            return 2;
        }
        int mc = str2mc(argv[2]);
        if (mc == MC_UNDEF) {
            unsupported("version_not_supported_by_cubiomes");
            return 2;
        }
        int64_t seed = strtoll(argv[3], NULL, 10);
        return measure_biome_area(mc, seed, argv[4], atoi(argv[5]), atoi(argv[6]), atoi(argv[7]), atoi(argv[8]));
    }

    if (!strcmp(kind, "biome_samples")) {
        if (argc != 10) {
            unsupported("usage: mc_query biome_samples version seed id center_x center_z radius samples limit");
            return 2;
        }
        int mc = str2mc(argv[2]);
        if (mc == MC_UNDEF) {
            unsupported("version_not_supported_by_cubiomes");
            return 2;
        }
        int64_t seed = strtoll(argv[3], NULL, 10);
        return sample_biome_candidates(mc, seed, argv[4], atoi(argv[5]), atoi(argv[6]), atoi(argv[7]), atoi(argv[8]), atoi(argv[9]));
    }

    if (argc != 9) {
        unsupported("usage: mc_query structure|biome version seed id center_x center_z radius limit");
        return 2;
    }

    int mc = str2mc(argv[2]);
    if (mc == MC_UNDEF) {
        unsupported("version_not_supported_by_cubiomes");
        return 2;
    }
    int64_t seed = strtoll(argv[3], NULL, 10);
    const char *id = argv[4];
    int cx = atoi(argv[5]);
    int cz = atoi(argv[6]);
    int radius = atoi(argv[7]);
    int limit = atoi(argv[8]);
    if (radius < 1) radius = 1;
    if (limit < 1) limit = 1;
    if (limit > 32768) limit = 32768;

    if (!strcmp(kind, "structure")) {
        return search_structure(mc, seed, id, cx, cz, radius, limit);
    }
    if (!strcmp(kind, "biome")) {
        return search_biome(mc, seed, id, cx, cz, radius, limit);
    }
    unsupported("unsupported_kind");
    return 2;
}
